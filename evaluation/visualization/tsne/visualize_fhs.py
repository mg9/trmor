# -----------------------------------------------------------
# Date:        2022/02/19 
# Author:      Muge Kural
# Description: Visualizations of final hidden states of trained models with t-SNE 
# -----------------------------------------------------------

from collections import defaultdict
from sklearn.datasets import fetch_openml
from sklearn.manifold import TSNE
from mpl_toolkits import mplot3d
from numpy import dot, save
from numpy.linalg import norm

import sys, argparse, random, torch, json, matplotlib, os
import torch.nn as nn
import numpy as np
import matplotlib.pyplot as plt
from torch.optim.lr_scheduler import ReduceLROnPlateau
from torch import optim
from common.utils import *
from data.data import build_data
from model.charlm.charlm import CharLM
from model.ae.ae import AE

from common.vocab import VocabEntry
#matplotlib.use('Agg')

# annotation on hovers
def update_annot(ind, params):
    fig, annot, ax, sc, words = params
    pos = sc.get_offsets()[ind["ind"][0]]
    annot.xy = pos
    text = "{}".format(" ".join([words[n] for n in ind["ind"]]))
    annot.set_text(text)
    #annot.get_bbox_patch().set_facecolor(cmap(norm(c[ind["ind"][0]])))
    annot.get_bbox_patch().set_alpha(0.4)

def hover(event, params):
    fig, annot, ax, sc, words = params
    vis = annot.get_visible()
    if event.inaxes == ax:
        cont, ind = sc.contains(event)
        if cont:
            update_annot(ind, params)
            annot.set_visible(True)
            fig.canvas.draw_idle()
        else:
            if vis:
                annot.set_visible(False)
                fig.canvas.draw_idle()

def config():
     # CONFIG
    parser = argparse.ArgumentParser(description='')
    args = parser.parse_args()
    args.device = 'cuda'
    model_id = 'ae_1'
    model_path, model_vocab  = get_model_info(model_id)
    # logging
    args.logdir = 'evaluation/visualization/tsne/results/'+model_id+'/'
    args.figfile   = args.logdir +'vis.png'
    try:
        os.makedirs(args.logdir)
        print("Directory " , args.logdir ,  " Created ") 
    except FileExistsError:
        print("Directory " , args.logdir ,  " already exists")
    # initialize model
    # load vocab (to initialize the model with correct vocabsize)
    with open(model_vocab) as f:
        word2id = json.load(f)
        args.vocab = VocabEntry(word2id)
    
    # model
    model_init = uniform_initializer(0.01); emb_init = uniform_initializer(0.1)
    args.ni = 512; #for ae,vae,charlm
    args.nz = 32   #for ae,vae
    args.enc_nh = 1024; args.dec_nh = 1024;  #for ae,vae
    args.nh = 1024 #for ae,vae,charlm
    args.enc_dropout_in = 0.0; args.enc_dropout_out = 0.0 #for ae,vae,charlm
    args.dec_dropout_in = 0.0; args.dec_dropout_out = 0.0 #for ae,vae
    args.model = AE(args, args.vocab, model_init, emb_init)

    # load model weights
    args.model.load_state_dict(torch.load(model_path))
    args.model.to(args.device)
    args.model.eval()
    # data
    #args.tstdata = 'evaluation/visualization/tsne/data/surf.uniquesurfs.trn.txt'
    args.tstdata = 'evaluation/visualization/tsne/data/pos(root)_verb.uniqueroots.trn.100.txt'

    args.maxtstsize = 100
    args.batch_size = 1
    return args


def main():
    args = config()
    data, batches = build_data(args)
    fhs_vectors = []; words = []
    with torch.no_grad():
        # loop through each instance
        for data in batches:
            # fhs: (1,1,nh)
            #fhs, _ = args.model(data) #charlm
            z, fhs = args.model.encoder(data) #ae
            fhs_vectors.append(z)
            word =''.join(args.vocab.decode_sentence(data[0][1:-1]))
            words.append(word)

    # (numinstances, nh)
    fhs_tensor = torch.stack(fhs_vectors).squeeze(1).squeeze(1).cpu()
    tsne_results = TSNE(n_components=2, verbose=1).fit_transform(fhs_tensor)
    fig,ax = plt.subplots()
    #ax.scatter(tsne_results[:,0], tsne_results[:,1])
    sc = plt.scatter(tsne_results[:,0], tsne_results[:,1])
    plt.savefig(args.figfile)

    annot = ax.annotate("", xy=(0,0), xytext=(20,20),textcoords="offset points", bbox=dict(boxstyle="round", fc="w"), arrowprops=dict(arrowstyle="->"))
    annot.set_visible(False)
    fig.canvas.mpl_connect("motion_notify_event", lambda event: hover(event, [fig, annot, ax, sc, words]))
    plt.show()


if __name__=="__main__":
    main()