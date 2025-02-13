import argparse
import os.path as osp

import torch
import torch.nn.functional as F
from torch.nn import Linear

import torch_geometric.transforms as T
from torch_geometric.datasets import MovieLens
from torch_geometric.nn import SAGEConv, to_hetero

parser = argparse.ArgumentParser()
parser.add_argument('--use_weighted_loss', action='store_true',
                    help='Whether to use weighted MSE loss.')
args = parser.parse_args()

device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

# preparing the data object for use as input to a PyTorch model
path = osp.join(osp.dirname(osp.realpath(__file__)), '../../data/MovieLens')
dataset = MovieLens(path, model_name='all-MiniLM-L6-v2')
data = dataset[0].to(device)                                      # extracts the first item from the MovieLens

# Add user node features for message passing:
data['user'].x = torch.eye(data['user'].num_nodes, device=device)    # creates a one-hot encoding of the nodes in the 'user' feature tensor
del data['user'].num_nodes                                           # (PyTorch Geometric) will rely on the shape of the 'x' attribute to determine the number of nodes

# Add a reverse ('movie', 'rev_rates', 'user') relation for message passing:
data = T.ToUndirected()(data)                                                 # converts the directed edges in the graph to undirected edges
del data['movie', 'rev_rates', 'user'].edge_label  # Remove "reverse" label.

# Perform a link-level split into training, validation, and test edges:
train_data, val_data, test_data = T.RandomLinkSplit(
    num_val=0.1,
    num_test=0.1,
    neg_sampling_ratio=0.0,                                              # A negative sample is a pair of nodes that are not connected by an edge
    edge_types=[('user', 'rates', 'movie')],                             # This means that the link prediction task will involve predicting which movies a user will rate, given their existing ratings
    rev_edge_types=[('movie', 'rev_rates', 'user')],                     # These edges represent the relationship between users and movies in the opposite direction (i.e., which users have rated a given movie)
)(data)

# We have an unbalanced dataset with many labels for rating 3 and 4, and very
# few for 0 and 1. Therefore we use a weighted MSE loss.
if args.use_weighted_loss:
    weight = torch.bincount(train_data['user', 'movie'].edge_label)
    weight = weight.max() / weight                                       # normalizes the weight tensor 
else:
    weight = None


def weighted_mse_loss(pred, target, weight=None):
    weight = 1. if weight is None else weight[target].to(pred.dtype)
    return (weight * (pred - target.to(pred.dtype)).pow(2)).mean()


class GNNEncoder(torch.nn.Module):                                # used to encode node features
    def __init__(self, hidden_channels, out_channels):
        super().__init__()
        self.conv1 = SAGEConv((-1, -1), hidden_channels)          # type of message-passing layer
        self.conv2 = SAGEConv((-1, -1), out_channels)

    def forward(self, x, edge_index):
        x = self.conv1(x, edge_index).relu()           # applies SAGEConv layer to the input tensor x using the edge index edge_index
        x = self.conv2(x, edge_index)
        return x                                       # the resulting tensor is the final output of the module

#  this module takes as input a dictionary of encoded node features and an edge label index tensor
#  and predicts the edge labels using a simple neural network architecture consisting of two linear layers
class EdgeDecoder(torch.nn.Module):                              # used to decode edge features in a graph
    def __init__(self, hidden_channels):
        super().__init__()
        self.lin1 = Linear(2 * hidden_channels, hidden_channels) # concatenate the node features of the two nodes in each edge
        self.lin2 = Linear(hidden_channels, 1)                   # 1:  we want to predict a single edge label

    def forward(self, z_dict, edge_label_index):
        row, col = edge_label_index
        z = torch.cat([z_dict['user'][row], z_dict['movie'][col]], dim=-1)

        z = self.lin1(z).relu()
        z = self.lin2(z)
        return z.view(-1)                                         # this line returns the predicted edge labels as a flattened tensor

# this module combines the GNNEncoder and EdgeDecoder modules to form a complete graph neural network model
class Model(torch.nn.Module):
    def __init__(self, hidden_channels):         # takes as input the hidden_channels hyperparameter, which determines the size of the hidden layer in the encoder
        super().__init__()
        self.encoder = GNNEncoder(hidden_channels, hidden_channels)
        self.encoder = to_hetero(self.encoder, data.metadata(), aggr='sum')     # converts the encoder module to a heterogeneous GNN module
        self.decoder = EdgeDecoder(hidden_channels)                             # initializes the decoder module with the specified number of hidden channels

    def forward(self, x_dict, edge_index_dict, edge_label_index):
        z_dict = self.encoder(x_dict, edge_index_dict)
        return self.decoder(z_dict, edge_label_index)                 # applies the decoder module to the encoded node features and edge label index of the input graph


model = Model(hidden_channels=32).to(device)        # this line of code is used to initialize the model before training or evaluating it

# Due to lazy initialization, we need to run one model step so the number
# of parameters can be inferred:
with torch.no_grad():             # it ensures that no gradients are computed during this forward pass, which is useful for efficiency reasons when you don't need to update the model parameters                
    model.encoder(train_data.x_dict, train_data.edge_index_dict)

optimizer = torch.optim.Adam(model.parameters(), lr=0.01)


def train():
    model.train()
    optimizer.zero_grad()
    pred = model(train_data.x_dict, train_data.edge_index_dict,
                 train_data['user', 'movie'].edge_label_index)
    target = train_data['user', 'movie'].edge_label
    loss = weighted_mse_loss(pred, target, weight)
    loss.backward()
    optimizer.step()
    return float(loss)


@torch.no_grad()
def test(data):
    model.eval()
    pred = model(data.x_dict, data.edge_index_dict,
                 data['user', 'movie'].edge_label_index)
    pred = pred.clamp(min=0, max=5)                         #  the predicted ratings are clamped between 0 and 5 using 
    target = data['user', 'movie'].edge_label.float()
    rmse = F.mse_loss(pred, target).sqrt()
    return float(rmse)


for epoch in range(1, 301):
    loss = train()
    train_rmse = test(train_data)
    val_rmse = test(val_data)
    test_rmse = test(test_data)
    print(f'Epoch: {epoch:03d}, Loss: {loss:.4f}, Train: {train_rmse:.4f}, '
          f'Val: {val_rmse:.4f}, Test: {test_rmse:.4f}')
