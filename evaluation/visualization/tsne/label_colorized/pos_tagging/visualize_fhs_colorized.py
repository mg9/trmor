# -----------------------------------------------------------
# Date:        2022/02/19 
# Author:      Muge Kural
# Description: Visualizations of final hidden states (points) of trained model with t-SNE and colorizes a point based on its pos-tag label 
# -----------------------------------------------------------

from collections import defaultdict
from sklearn.datasets import fetch_openml
from sklearn.manifold import TSNE
from mpl_toolkits import mplot3d
from numpy import dot, save
from numpy.linalg import norm
import matplotlib.patches as mpatches
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
    probe_id = 'ae_1_probe_pos_tagging'
    _, (_, surfpos_vocab)  = get_model_info(probe_id) 

    # logging
    args.logdir = 'evaluation/visualization/tsne/label_colorized/pos_tagging/results/'+model_id+'/'
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
        args.surf_vocab = VocabEntry(word2id)
    # load vocab (to initialize the model with correct vocabsize)
    with open(surfpos_vocab) as f:
        word2id = json.load(f)
        args.surfpos_vocab = VocabEntry(word2id)
    args.vocab = (args.surf_vocab, args.surfpos_vocab)

    # colors
    with open('evaluation/visualization/colors.json', 'r') as f:
        args.color_dict = json.load(f)
  
    # model
    model_init = uniform_initializer(0.01); emb_init = uniform_initializer(0.1)
    args.ni = 512; #for ae,vae,charlm
    args.nz = 32   #for ae,vae
    args.enc_nh = 1024; args.dec_nh = 1024;  #for ae,vae
    args.nh = 1024 #for ae,vae,charlm
    args.enc_dropout_in = 0.0; args.enc_dropout_out = 0.0 #for ae,vae,charlm
    args.dec_dropout_in = 0.0; args.dec_dropout_out = 0.0 #for ae,vae
    args.model = AE(args, args.surf_vocab, model_init, emb_init)
    # load model weights
    args.model.load_state_dict(torch.load(model_path))
    args.model.to(args.device)
    args.model.eval()
    # data
    #args.tstdata = 'evaluation/visualization/tsne/label_colorized/pos_tagging/data/surf.uniquesurfs.trn.txt'
    args.tstdata = 'evaluation/visualization/tsne/data/pos(root)_verb.uniqueroots.trn.100.txt'
    args.maxtstsize = 10000
    args.batch_size = 1
    return args


def main():
    args = config()
    data, batches = build_data(args)
    fhs_vectors = []; words = []; label_colors =[]
    with torch.no_grad():
        # loop through each instance
        for data in batches:
            surf, surfpos = data
            # fhs: (1,1,nh)
            #fhs, _ = args.model(surf) #charlm
            _, fhs = args.model.encoder(surf) #ae
            fhs_vectors.append(fhs)
            word =''.join(args.surf_vocab.decode_sentence(surf[0][1:-1]))
            words.append(word)
            label_colors.append( args.color_dict[str(surfpos.item())])
            
    # (numinstances, nh)
    fhs_tensor = torch.stack(fhs_vectors).squeeze(1).squeeze(1).cpu()
    tsne_results = TSNE(n_components=2, verbose=1).fit_transform(fhs_tensor)
    fig,ax = plt.subplots()
    # hande color legends
    color_handles = []
    for cid, color in args.color_dict.items(): 
        if color not in label_colors:
            continue 
        color_patch = mpatches.Patch(color=color, label=args.surfpos_vocab.id2word(int(cid)))
        color_handles.append(color_patch)
    ax.legend(handles=color_handles)
    sc = plt.scatter(tsne_results[:,0], tsne_results[:,1], color=label_colors)
    plt.savefig(args.figfile)
    annot = ax.annotate("", xy=(0,0), xytext=(20,20),textcoords="offset points", bbox=dict(boxstyle="round", fc="w"), arrowprops=dict(arrowstyle="->"))
    annot.set_visible(False)
    fig.canvas.mpl_connect("motion_notify_event", lambda event: hover(event, [fig, annot, ax, sc, words]))
    plt.show()


if __name__=="__main__":
    main()