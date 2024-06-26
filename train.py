# pip install torch==2.1.2 torchvision==0.16.2 torchaudio==2.1.2 xformers==0.0.23post1 mwparserfromhell datasets fast-pytorch-kmeans
# wandb key: 4d89c43f67fc55f37cc6e65e9304ef29b1a454f3
import torch
import numpy as np
import time
import data
import torch.nn.functional as F
import modeling
import tqdm
from sklearn.cluster import MiniBatchKMeans
from fast_pytorch_kmeans import KMeans
# from torchviz import make_dot
import wandb
import os


DEBUG=True
USE_WANDB=False


def load_model(checkpoint_dir, model, model_name="encoder"):
    """
    Load the model from the latest checkpoint.
    """
    checkpoints = [f for f in os.listdir(checkpoint_dir) if f.startswith(model_name) and f.endswith(".pt")]
    if checkpoints:
        # Sort files by their step number
        checkpoints.sort(key=lambda f: int(f.split('_')[-1].split('.')[0]))
        latest_checkpoint = os.path.join(checkpoint_dir, checkpoints[-1])
        model.load_state_dict(torch.load(latest_checkpoint))
        step_number = int(checkpoints[-1].split('_')[-1].split('.')[0])
        print(f"Restored {model_name} from {latest_checkpoint}")
        return model, step_number
    else:
        print(f"No checkpoints found for {model_name} in {checkpoint_dir}. Starting from scratch.")
        return model, 0


def manage_checkpoints(checkpoint_dir, max_checkpoints=5):
    # Get all checkpoint files
    checkpoints = [f for f in os.listdir(checkpoint_dir) if f.endswith(".pt")]
    
    # If there are more than `max_checkpoints` files, remove the oldest
    if len(checkpoints) > max_checkpoints:
        # Sort files by their creation time
        checkpoints.sort(key=lambda f: os.path.getctime(os.path.join(checkpoint_dir, f)))
        # Remove the oldest
        for f in checkpoints[:-max_checkpoints]:
            os.remove(os.path.join(checkpoint_dir, f))
            print(f"Removed old checkpoint: {f}")


def save_model(encoder_model, decoder_model, checkpoint_dir, step_number):
    encoder_path = os.path.join(checkpoint_dir, f"encoder_model_step_{step_number}.pt")
    decoder_path = os.path.join(checkpoint_dir, f"decoder_model_step_{step_number}.pt")

    torch.save(encoder_model.state_dict(), encoder_path)
    torch.save(decoder_model.state_dict(), decoder_path)
    print(f"Saved models at step {step_number} to {checkpoint_dir}")


def kmeans_features(encoder_model, train_data, vocab_size, max_gather_steps,
                    max_kmeans_steps, torch_kmeans=True):
    encoder_model.eval()  # turn on train mode
    pred_vectors = []
    for batch_idx, batch in tqdm.tqdm(enumerate(train_data)):
        if batch_idx > max_gather_steps:
            break
        device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
        batch = batch.to(device)
        soft_preds = encoder_model(batch) 
        soft_preds = soft_preds.view(-1, soft_preds.shape[-1])
        soft_preds = soft_preds.detach().cpu().numpy()
        pred_vectors.extend(soft_preds)
    n_clusters = vocab_size
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    start = time.time()
    if not torch_kmeans:
        print(f"Running mini batch kmeans on {len(pred_vectors)} vectors with {vocab_size} centroids...")
        kmeans = MiniBatchKMeans(n_clusters=n_clusters, batch_size=10_000, random_state=42,
                                 max_iter=max_kmeans_steps, verbose=10**10)
        kmeans.fit(pred_vectors)
        centroids = kmeans.cluster_centers_
        centroids = torch.from_numpy(centroids).float().to(device)
    else:
        print(f"Running torch kmeans on {len(pred_vectors)} vectors with {vocab_size} centroids...")
        kmeans = KMeans(n_clusters=vocab_size, mode='euclidean', verbose=1, max_iter=max_kmeans_steps,
                        minibatch=min(100_000, len(pred_vectors)))
        kmeans.fit_predict(torch.tensor(np.array(pred_vectors)).to(device))
        centroids = kmeans.centroids
    end = time.time()
    print(f"KMeans took {end - start} seconds.")
    return centroids


def diversity_loss(vectors, subsample_size=1000):
    """
    Compute the diversity loss for a batch of vectors with random subsampling to avoid large similarity matrix computations.
    
    Args:
    - vectors (Tensor): A 3D tensor of shape (batch_size, sequence_dim, vector_dim) where each row is a vector.
    - subsample_size (int): The number of vectors to randomly subsample for the diversity calculation.
    
    Returns:
    - loss (Tensor): A scalar tensor representing the diversity loss.
    """
    batch_size, seq_dim, vector_dim = vectors.shape

    # Reshape to treat each vector in the sequence separately
    vectors = vectors.reshape(batch_size * seq_dim, vector_dim)
    
    # Randomly subsample vectors to reduce size
    total_vectors = vectors.shape[0]
    subsample_indices = torch.randperm(total_vectors)[:subsample_size]
    vectors_subsampled = vectors[subsample_indices]

    # Normalize the subsampled vectors to unit length
    vectors_norm = F.normalize(vectors_subsampled, p=2, dim=1)
    
    # Compute the cosine similarity matrix for the subsampled set
    similarity_matrix = torch.matmul(vectors_norm, vectors_norm.T)
    
    # Zero out the diagonal (self-similarity) by subtracting it out
    eye = torch.eye(vectors_subsampled.shape[0], device=vectors.device)
    similarity_matrix = similarity_matrix - eye
    
    # Since we want to minimize similarity, we take the sum of all positive similarities
    positive_similarities = torch.relu(similarity_matrix)
    loss = positive_similarities.sum() / (vectors_subsampled.shape[0] * (vectors_subsampled.shape[0] - 1))

    return loss


def evaluate(encoder_model, decoder_model, eval_data, criterion,
             num_evals=1, print_predictions=True, samples_to_print=1,
             use_vq: bool = False):
    encoder_model.eval()  # Turn on evaluation mode
    decoder_model.eval()  # Turn on evaluation mode

    total_loss = 0
    correct_predictions = 0
    total_predictions = 0

    with torch.no_grad():  # No need to track gradients
        for batch_idx, batch in enumerate(eval_data):
            if batch_idx >= num_evals:  # Ensure it breaks at num_evals
                break
            device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
            batch = batch.to(device)
            T = batch.shape[1]  # Assuming T is the sequence length from inputs
            if use_vq:
                hard_preds_st, _, _, _ = encoder_model(batch) 
                reconstructed = decoder_model(hard_preds_st)
            else:
                soft_preds = encoder_model(batch) 
                reconstructed = decoder_model(soft_preds)

            num_classes = reconstructed.shape[-1]
            reconstructed_flat = reconstructed.view(-1, num_classes)
            targets_flat = batch.view(-1).long()

            loss = criterion(reconstructed_flat, targets_flat)
            total_loss += loss.item()
            _, predicted_labels = torch.max(reconstructed_flat, 1)
            correct_predictions += (predicted_labels == targets_flat).sum().item()
            total_predictions += targets_flat.size(0)

            # Optionally print predictions and ground truths
            if print_predictions and batch_idx < samples_to_print:
                predicted_labels_reshaped = predicted_labels.view(batch.shape[0], T)
                print("Batch", batch_idx)
                for i in range(min(len(batch), samples_to_print)):
                    print(f"Ground Truth: {','.join(map(str, batch[i].tolist()))}")
                    print(f"Prediction:  {','.join(map(str, predicted_labels_reshaped[i].tolist()))}\n")
                    # print(f"Soft prediction:  {''.join(map(str, soft_prediction[i].tolist()))}\n")

    avg_loss = total_loss / min(batch_idx + 1, num_evals)
    accuracy = correct_predictions / total_predictions * 100
    if USE_WANDB:
        wandb.log({'eval_loss': avg_loss, 'eval_accuracy': accuracy})
    print(f'Evaluation - Loss: {avg_loss:.4f}, Accuracy: {accuracy:.2f}%')

    return avg_loss, accuracy


def train(encoder_model, decoder_model, train_data, criterion,
          optimizer, log_interval=1, max_steps=100, start_step=0,
          diversity_weight=1.0,
          use_vq: bool = False):
    encoder_model.train()  # turn on train mode
    decoder_model.train()  # turn on train mode
    criterion = torch.nn.CrossEntropyLoss()
    losses = {
        'loss_recon': [],
        'vq_loss': [],
        'commit_loss': [],
        'diversity_loss': [],
    }
    for batch_idx, batch in enumerate(train_data):
        if batch_idx + start_step > max_steps:
            break

        device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
        batch = batch.to(device)
        # Converting binary tokens into vectors.
        # Input from batch is 0s and 1s of shape [batch_size, T]
        # Output shape should be [batch_size, T, d_model]
        optimizer.zero_grad()

        if use_vq:
            hard_preds_st, hard_preds, soft_preds, tokens = encoder_model(batch) 
            reconstructed = decoder_model(hard_preds_st)
            loss_div = diversity_loss(soft_preds) * diversity_weight
            loss_vq = F.mse_loss(hard_preds, soft_preds.detach())
            loss_commit = F.mse_loss(soft_preds, hard_preds.detach())
        else:
            soft_preds = encoder_model(batch) 
            reconstructed = decoder_model(soft_preds)
            loss_div = torch.zeros(1, device=device)
            loss_vq = torch.zeros(1, device=device)
            loss_commit = torch.zeros(1, device=device)
            tokens = '-1'

        num_classes = reconstructed.shape[-1]
        loss_recon = criterion(reconstructed.view(-1, num_classes), batch.view(-1).long())
        # TODO add loss weights
        loss = loss_recon + loss_vq + loss_commit + loss_div
        loss.backward()
        torch.nn.utils.clip_grad_norm_(list(encoder_model.parameters()) + list(decoder_model.parameters()), 0.5)
        optimizer.step()
        if batch_idx % log_interval == 0:  # log_interval could be, e.g., 10
            print(f'Batch: {batch_idx + start_step}, Loss: {loss.item()}, '
                  f'Recon loss: {loss_recon.item()}, VQ loss: {loss_vq.item()}, Commit loss: {loss_commit.item()}, '
                  f'Diversity loss: {loss_div.item()}')
            if batch_idx % 100 == 0:
                reconstructed_flat = reconstructed.view(-1, num_classes)
                _, predicted_labels = torch.max(reconstructed_flat, 1)
                predicted_labels_reshaped = predicted_labels.view(batch.shape[0], batch.shape[1])
                print("Ground truth:", batch[0])
                print("Latent prediction:", tokens[0])
                print("Reconstructed prediction:", predicted_labels_reshaped[0])
            losses['loss_recon'].append(loss_recon.item())
            losses['vq_loss'].append(loss_vq.item())
            losses['commit_loss'].append(loss_commit.item())
            losses['diversity_loss'].append(loss_div.item())
            if USE_WANDB:
                wandb.log({'train_loss_recon': loss_recon.item(),
                           'train_vq_loss': loss_vq.item(),
                           'train_commit_loss': loss_commit.item(),
                           'train_diversity_loss': loss_div.item()})
    return {k: np.mean(v) for k, v in losses.items()}

def main():
    if DEBUG:
        config = {
            # Load data
            'chunk_size': 120, # Encode 8 bytes sequence length.
            'split_percentage':0.8, # Use 80% of data for training.
            'batch_size': 32,
            # model hypers
            'lr':1e-3,
            'diversity_weight': 1.0,
            'ntokens':256,  # All bytes.
            'd_model': 300,
            'd_hid':512,  # dimension of the feedforward network model in ``nn.TransformerEncoder``
            'nlayers':4,  # number of ``nn.TransformerEncoderLayer`` in ``nn.TransformerEncoder``
            'nhead': 4,  # number of heads in ``nn.MultiheadAttention``
            'dropout': 0.2,  # dropout probability
            'num_latent_vectors': 8000,
            'use_bits': False,
            'compression_factor': 4,
            'use_vq': False,
            'steps_before_vq': 500,
            'kmeans_gather_steps': 25,
            'kmeans_steps': 25,
            'eval_every': 100,
            'version': 'wiki',
            'kmeans_algo': 'sklearn',
            'restore_dir': '/tmp/local_run_continuous_checkpoints',
        }
    else:
        config = {
            # Load data
            'chunk_size': 1024, # Encode 8 bytes sequence length.
            'split_percentage': 0.8, # Use 80% of data for training.
            'batch_size': 128,
            'lr': 1e-4,
            'diversity_weight': 50.0,
            # model hypers
            'ntokens': 256,  # All bytes.
            'd_model': 512,
            'd_hid': 512,  # dimension of the feedforward network model in ``nn.TransformerEncoder``
            'nlayers':4,  # number of ``nn.TransformerEncoderLayer`` in ``nn.TransformerEncoder``
            'nhead': 4,  # number of heads in ``nn.MultiheadAttention``
            'dropout': 0.2,  # dropout probability
            'num_latent_vectors': 24_000,
            'use_bits': False,
            'compression_factor': 8,
            'steps_before_vq': 2000,
            'kmeans_gather_steps': 500,
            'kmeans_steps': 20,
            'eval_every': 1000,
            'version': 'wiki',
            'kmeans_algo': 'torch',
            'restore_dir': None,
        }
    # start a new wandb run to track this script
    if USE_WANDB:
        wandb.init(
            # set the wandb project where this run will be logged
            project="rackitten-tokenizer",
            # track hyperparameters and run metadata
            config=config
        )
        run_id = wandb.run.id
        print("Run id is: ", run_id)
    else:
        run_id = "local_run"
    train_data, eval_data = data.create_data_loaders(
        chunk_size=config['chunk_size'],
        split_percentage=config['split_percentage'],
        batch_size=config['batch_size'],
        use_bits=config['use_bits'],
        version=config['version'])
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    assert config['nlayers'] % 2 == 0
    encoder_model = modeling.PoolExpandTransformerModel(
        ntoken=config['ntokens'],
        d_model=config['d_model'],
        nhead=config['nhead'],
        d_hid=config['d_hid'],
        nlayers_pre=config['nlayers'] // 2,
        nlayers_post=config['nlayers'] // 2,
        dropout=config['dropout'],
        include_linear=False,
        use_vq=False,
        num_latent_vectors=config['num_latent_vectors'],
        max_len=config['chunk_size'],
        compression_factor=config['compression_factor']).to(device)
    assert config['d_model'] % config['compression_factor'] == 0
    decoder_model = modeling.PoolExpandTransformerModel(
        ntoken=config['ntokens'],
        d_model=config['d_model'],
        d_hid=config['d_hid'],
        nlayers_pre=config['nlayers'] // 2,
        nlayers_post=config['nlayers'] // 2,
        nhead=config['nhead'],
        dropout=config['dropout'],
        include_linear=True,
        vector_input=True,
        use_vq=False,
        max_len=config['chunk_size'],
        compression_factor=1./config["compression_factor"]).to(device)

    # Penalize the model for reconstructing the binary input.
    criterion = torch.nn.CrossEntropyLoss()
    # TODO: Optimizer params not persisted or restored. This causes loss
    # spikes on restoration.
    optimizer = torch.optim.Adam(list(encoder_model.parameters()) + list(decoder_model.parameters()), lr=config['lr'])

    discrete_checkpoint_dir = f'/tmp/{run_id}_discrete_checkpoints'
    continuous_checkpoint_dir = f'/tmp/{run_id}_continuous_checkpoints'
    os.makedirs(continuous_checkpoint_dir, exist_ok=True)
    os.makedirs(discrete_checkpoint_dir, exist_ok=True)

    steps = 0

    if config['restore_dir'] is not None and 'continuous' in config['restore_dir']:
        encoder_model, steps = load_model(config['restore_dir'], encoder_model, model_name="encoder")
        decoder_model, dec_steps = load_model(config['restore_dir'], decoder_model, model_name="decoder")
        assert steps == dec_steps
        print(f"Restored continuous model from {config['restore_dir']} at step {steps}")

    if config['restore_dir'] is None or steps < config['steps_before_vq']: 
        print("Training continuous model...")
        continuous_losses = train(
            encoder_model=encoder_model,
            decoder_model=decoder_model,
            train_data=train_data,
            criterion=criterion, optimizer=optimizer,
            start_step=steps, max_steps=config['steps_before_vq'],
            diversity_weight=config['diversity_weight'],
            use_vq=False)
        steps = config['steps_before_vq']
        print("Saving continuous model...")
        save_model(encoder_model, decoder_model, continuous_checkpoint_dir, steps)
        cont_avg_loss, cont_accuracy = evaluate(encoder_model, decoder_model, eval_data,
                                                criterion=criterion, use_vq=False)

    if config['restore_dir'] is not None and 'discrete' in config['restore_dir']:
        encoder_model, steps = load_model(config['restore_dir'], encoder_model, model_name="encoder")
        decoder_model, dec_steps = load_model(config['restore_dir'], decoder_model, model_name="decoder")
        assert steps == dec_steps
        encoder_model.use_vq = True
        print(f"Restored discrete model from {config['restore_dir']} at step {steps}")
    else:
        assert config['kmeans_algo'] in ['torch', 'sklearn']
        init_codebook = kmeans_features(encoder_model=encoder_model,
                                        train_data=train_data,
                                        vocab_size=config['num_latent_vectors'],
                                        max_gather_steps=config['kmeans_gather_steps'],
                                        max_kmeans_steps=config['kmeans_steps'],
                                        torch_kmeans=config['kmeans_algo'] == 'torch')
        encoder_model.use_vq = True
        encoder_model.set_codebook(init_codebook)
    for _ in range(100): # Pretrain continuous
        train_losses = train(encoder_model, decoder_model, train_data,
                             criterion=criterion, optimizer=optimizer,
                             start_step=steps, max_steps=steps + config['eval_every'],
                             diversity_weight=config['diversity_weight'],
                             use_vq=True)
        steps += config['eval_every']
        avg_loss, accuracy = evaluate(encoder_model, decoder_model, eval_data, criterion=criterion, use_vq=True)
        print("Saving model....")
        save_model(encoder_model, decoder_model, discrete_checkpoint_dir, steps)
        # manage_checkpoints(discrete_checkpoint_dir)  
    wandb.finish()
if __name__ == "__main__":
    main()  # Call the main function
