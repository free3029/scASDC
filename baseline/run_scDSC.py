import argparse
import random
import numpy as np
from sklearn.cluster import KMeans
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.nn.parameter import Parameter
from torch.optim import Adam
from torch.nn import Linear
from utils import load_data, load_graph, RAdam
from GNN import GNNLayer
from evaluation import eva
from torch.utils.data import DataLoader, TensorDataset
import h5py
import scanpy as sc
from preprocess import read_dataset, normalize
from layers import ZINBLoss, MeanAct, DispAct
from torch.autograd import Variable
import os
from preprocess import prepro, normalize_1
from calcu_graph import construct_graph
from sklearn.metrics import adjusted_rand_score as ari_score
import warnings
import umap
import matplotlib.pyplot as plt

warnings.filterwarnings("ignore", category=UserWarning)
warnings.filterwarnings("ignore", category=FutureWarning)

os.environ["CUDA_VISIBLE_DEVICES"] = "0"
os.environ['CUDA_LAUNCH_BLOCKING'] = "1"
from time import time as get_time

torch.cuda.set_device(0)


class AE(nn.Module):
    def __init__(self, n_enc_1, n_enc_2, n_enc_3, n_dec_1, n_dec_2, n_dec_3,
                 n_input, n_z1, n_z2, n_z3):
        super(AE, self).__init__()

        self.enc_1 = Linear(n_input, n_enc_1)
        self.BN1 = nn.BatchNorm1d(n_enc_1)
        self.enc_2 = Linear(n_enc_1, n_enc_2)
        self.BN2 = nn.BatchNorm1d(n_enc_2)
        self.enc_3 = Linear(n_enc_2, n_enc_3)
        self.BN3 = nn.BatchNorm1d(n_enc_3)

        self.z1_layer = Linear(n_enc_3, n_z1)
        self.BN4 = nn.BatchNorm1d(n_z1)
        self.z2_layer = Linear(n_z1, n_z2)
        self.BN5 = nn.BatchNorm1d(n_z2)
        self.z3_layer = Linear(n_z2, n_z3)
        self.BN6 = nn.BatchNorm1d(n_z3)

        self.dec_1 = Linear(n_z3, n_dec_1)
        self.BN7 = nn.BatchNorm1d(n_dec_1)
        self.dec_2 = Linear(n_dec_1, n_dec_2)
        self.BN8 = nn.BatchNorm1d(n_dec_2)
        self.dec_3 = Linear(n_dec_2, n_dec_3)
        self.BN9 = nn.BatchNorm1d(n_dec_3)
        self.x_bar_layer = Linear(n_dec_3, n_input)

    def forward(self, x):
        enc_h1 = F.relu(self.BN1(self.enc_1(x)))
        enc_h2 = F.relu(self.BN2(self.enc_2(enc_h1)))
        enc_h3 = F.relu(self.BN3(self.enc_3(enc_h2)))

        z1 = self.BN4(self.z1_layer(enc_h3))
        z2 = self.BN5(self.z2_layer(z1))
        z3 = self.BN6(self.z3_layer(z2))

        dec_h1 = F.relu(self.BN7(self.dec_1(z3)))
        dec_h2 = F.relu(self.BN8(self.dec_2(dec_h1)))
        dec_h3 = F.relu(self.BN9(self.dec_3(dec_h2)))
        x_bar = self.x_bar_layer(dec_h3)

        return x_bar, enc_h1, enc_h2, enc_h3, z3, z2, z1, dec_h3


class SDCN(nn.Module):
    def __init__(self, n_enc_1, n_enc_2, n_enc_3, n_dec_1, n_dec_2, n_dec_3,
                 n_input, n_z1, n_z2, n_z3, n_clusters, v=1):
        super(SDCN, self).__init__()
        self.ae = AE(
            n_enc_1=n_enc_1,
            n_enc_2=n_enc_2,
            n_enc_3=n_enc_3,

            n_dec_1=n_dec_1,
            n_dec_2=n_dec_2,
            n_dec_3=n_dec_3,

            n_input=n_input,
            n_z1=n_z1,
            n_z2=n_z2,
            n_z3=n_z3,
        )
        self.ae.load_state_dict(torch.load(args.pretrain_path, map_location='cpu'))
        self.gnn_1 = GNNLayer(n_input, n_enc_1)
        self.gnn_2 = GNNLayer(n_enc_1, n_enc_2)
        self.gnn_3 = GNNLayer(n_enc_2, n_enc_3)
        self.gnn_4 = GNNLayer(n_enc_3, n_z1)
        self.gnn_5 = GNNLayer(n_z1, n_z2)
        self.gnn_6 = GNNLayer(n_z2, n_z3)
        self.gnn_7 = GNNLayer(n_z3, n_clusters)

        # cluster layer
        self.cluster_layer = Parameter(torch.Tensor(n_clusters, n_z3))
        torch.nn.init.xavier_normal_(self.cluster_layer.data)
        self._dec_mean = nn.Sequential(nn.Linear(n_dec_3, n_input), MeanAct())
        self._dec_disp = nn.Sequential(nn.Linear(n_dec_3, n_input), DispAct())
        self._dec_pi = nn.Sequential(nn.Linear(n_dec_3, n_input), nn.Sigmoid())
        # degree
        self.v = v
        self.zinb_loss = ZINBLoss().cuda()

    def forward(self, x, adj):
        # DNN Module
        x_bar, tra1, tra2, tra3, z3, z2, z1, dec_h3 = self.ae(x)

        sigma = 0.5
        # GCN Module
        h = self.gnn_1(x, adj)
        h = self.gnn_2((1 - sigma) * h + sigma * tra1, adj)
        h = self.gnn_3((1 - sigma) * h + sigma * tra2, adj)
        h = self.gnn_4((1 - sigma) * h + sigma * tra3, adj)
        h = self.gnn_5((1 - sigma) * h + sigma * z1, adj)
        h = self.gnn_6((1 - sigma) * h + sigma * z2, adj)
        h = self.gnn_7((1 - sigma) * h + sigma * z3, adj, active=False)

        predict = F.softmax(h, dim=1)

        _mean = self._dec_mean(dec_h3)
        _disp = self._dec_disp(dec_h3)
        _pi = self._dec_pi(dec_h3)
        zinb_loss = self.zinb_loss

        q = 1.0 / (1.0 + torch.sum(torch.pow(z3.unsqueeze(1) - self.cluster_layer, 2), 2) / self.v)
        q = q.pow((self.v + 1.0) / 2.0)
        q = (q.t() / torch.sum(q, 1)).t()

        return x_bar, q, predict, z3, _mean, _disp, _pi, zinb_loss


def target_distribution(q):
    weight = q ** 2 / q.sum(0)
    return (weight.t() / weight.sum(1)).t()


def train_sdcn(dataset, X_raw, sf):
    global p
    model = SDCN(
        n_enc_1=args.n_enc_1,
        n_enc_2=args.n_enc_2,
        n_enc_3=args.n_enc_3,
        n_dec_1=args.n_dec_1,
        n_dec_2=args.n_dec_2,
        n_dec_3=args.n_dec_3,
        n_input=args.n_input,
        n_z1=args.n_z1,
        n_z2=args.n_z2,
        n_z3=args.n_z3,
        n_clusters=args.n_clusters,
        v=1).to(device)
    print(model)
    # optimizer = Adam(model.parameters(), lr=args.lr)
    optimizer = RAdam(model.parameters(), lr=args.lr)

    adj = load_graph(args.graph, args.k, dataset.x.shape[0])
    print(args.k)
    adj = adj.cuda()

    data = torch.Tensor(dataset.x).to(device)
    y = dataset.y

    with torch.no_grad():
        _, _, _, _, z, _, _, _ = model.ae(data)

    kmeans = KMeans(n_clusters=args.n_clusters, n_init=args.n_init)
    y_pred = kmeans.fit_predict(z.data.cpu().numpy())
    y_pred_last = y_pred
    model.cluster_layer.data = torch.tensor(kmeans.cluster_centers_).to(device)
    eva(y, y_pred, 0)

    for epoch in range(200):
        if epoch % 1 == 0:
            _, tmp_q, pred, _, _, _, _, _ = model(data, adj)
            tmp_q = tmp_q.data
            p = target_distribution(tmp_q)

            res1 = tmp_q.cpu().numpy().argmax(1)  # Q
            res2 = pred.data.cpu().numpy().argmax(1)  # Z
            res3 = p.data.cpu().numpy().argmax(1)  # P
            # eva(y, res1, str(epoch) + 'Q')
            # eva(y, res2, str(epoch) + 'Z')
            # eva(y, res3, str(epoch) + 'P')
            eva(y, res2, epoch)
            if epoch == 199:
                acc, nmi, ari, f1 = eva(y, res2, str(epoch) + 'Z')

        x_bar, q, pred, z, meanbatch, dispbatch, pibatch, zinb_loss = model(data, adj)

        binary_crossentropy_loss = F.kl_div(q.log(), p, reduction='batchmean')
        ce_loss = F.kl_div(pred.log(), p, reduction='batchmean')
        re_loss = F.mse_loss(x_bar, data)
        X_raw = torch.as_tensor(X_raw).cuda()
        sf = torch.as_tensor(sf).cuda()

        zinb_loss = zinb_loss(X_raw, meanbatch, dispbatch, pibatch, sf)
        loss = Balance_para[0] * binary_crossentropy_loss + Balance_para[1] * ce_loss + Balance_para[2] * re_loss + \
               Balance_para[3] * zinb_loss

        optimizer.zero_grad()
        loss.backward()
        optimizer.step()
    return acc, nmi, ari, f1


if __name__ == "__main__":
    dataname = 'Quake_10x_Limb_Muscle'
    num_clu = 6
    high_genes = 2000
    time_start = get_time()
    parser = argparse.ArgumentParser(
        description='train',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    model_para = [1000, 1000, 4000]
    Para = [2048, 1e-4, 80]
    Cluster_para = [11, 2000, 500, 10, 2000, 20]
    Balance_para = [0.1, 0.01, 1, 0.1, 0.5]
    parser.add_argument('--name', type=str, default=dataname)
    parser.add_argument('--graph', type=str, default=dataname)
    parser.add_argument('--pretrain_path', type=str, default='{}_scDSC.pkl'.format(dataname))
    parser.add_argument('--n_enc_1', default=model_para[0], type=int)
    parser.add_argument('--n_enc_2', default=model_para[1], type=int)
    parser.add_argument('--n_enc_3', default=model_para[2], type=int)
    parser.add_argument('--n_dec_1', default=model_para[2], type=int)
    parser.add_argument('--n_dec_2', default=model_para[1], type=int)
    parser.add_argument('--n_dec_3', default=model_para[0], type=int)

    parser.add_argument('--k', type=int, default=10)
    parser.add_argument('--lr', type=float, default=1e-4)
    # todo修改聚类数
    parser.add_argument('--n_clusters', default=num_clu, type=int)
    parser.add_argument('--n_z1', default=2000, type=int)
    parser.add_argument('--n_z2', default=500, type=int)
    parser.add_argument('--n_z3', default=10, type=int)
    # todo修改n_input，和高变基因数相等
    parser.add_argument('--n_input', type=int, default=high_genes)
    parser.add_argument('--n_init', type=int, default=20)

    args = parser.parse_args()
    args.cuda = torch.cuda.is_available()
    print("use cuda: {}".format(args.cuda))

    device = torch.device("cuda" if args.cuda else "cpu")
    args.pretrain_path = '../datasets/{}_scDSC.pkl'.format(dataname)
    # todo修改路径
    x, y = prepro('../datasets/{}/data.h5'.format(dataname))
    x = np.ceil(x).astype(int)
    adata = sc.AnnData(x)
    adata.obs['Group'] = y
    adata = normalize_1(adata, copy=True, highly_genes=high_genes, size_factors=True, normalize_input=True,
                        logtrans_input=True)
    count = adata.X
    # 这里建图
    construct_graph(count, y, 'ncos', name=dataname)
    dataset = load_data(count, y)
    X_raw = adata.raw.X
    sf = adata.obs.size_factors
    dataset1 = TensorDataset(torch.Tensor(count), torch.Tensor(X_raw), torch.Tensor(sf))
    dataloader = DataLoader(dataset1, batch_size=Para[0], shuffle=True)
    acc_li = []
    nmi_li = []
    ari_li = []
    f1_li = []
    for i in range(5):
        print('第', i, '次实验')
        print('--------------------------------------------------------')
        acc, nmi, ari, f1 = train_sdcn(dataset, count, sf)
        acc_li.append(acc)
        nmi_li.append(nmi)
        ari_li.append(ari)
        f1_li.append(f1)
    np.savez("results/scDSC_{}.npz".format(dataname), ARI=ari_li, NMI=nmi_li, ACC=acc_li, f1=f1_li)
    loaded_data = np.load('results/scDSC_{}.npz'.format(dataname))
    acc1 = loaded_data['ACC']
    nmi1 = loaded_data['NMI']
    ari1 = loaded_data['ARI']
    f11 = loaded_data['f1']

    combined_array = np.column_stack((acc1, nmi1, ari1, f11))
    np.savetxt('results/scDSC_{}_res.csv'.format(dataname), combined_array, delimiter=',')

