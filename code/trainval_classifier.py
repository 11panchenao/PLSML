import numpy as np
import os
import time
import torch
from tqdm import tqdm
from utils import save_itk, load_itk_image, dice_coef_np, ppv_np, \
    sensitivity_np, specificity_np, combine_total_avg, combine_total, normalize_min_max
from loss import dice_loss1, binary_cross_entropy, focal_loss, NT_Xent,softmax_mse_loss,dice_loss
from torch.cuda import empty_cache
import csv
import gc
import torch.nn.functional as F
from loss import CriterionPairWiseforWholeFeatAfterPool,entropy_loss,selfinformation,softmax_dice_loss
import torch
import torch.nn.functional as  F
import torch.nn as nn
import math
import numpy as np

from networks.vnet import VNet
from networks.ResNet34 import Resnet34
from utils2 import ramps,losses
# from scipy.ndimage.interpolation import zoom
# import skimage.measure as measure
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
preprocessed_data_dir = os.path.join(BASE_DIR, 'preprocessed_data')
th_bin = 0.5

CE_LOSS =nn.CrossEntropyLoss(reduction='none')
def sigmoid_rampup(current, rampup_length):
    """Exponential rampup from https://arxiv.org/abs/1610.02242"""
    if rampup_length == 0:
        return 1.0
    else:
        current = np.clip(current, 0.0, rampup_length)
        phase = 1.0 - current / rampup_length
        return float(np.exp(-5.0 * phase * phase))
def update_ema_variables(model, ema_model, alpha, global_step):
    # Use the true average until the exponential average is more correct
    alpha = min(1 - 1 / (global_step + 1), alpha)
    for ema_param, param in zip(ema_model.parameters(), model.parameters()):
        ema_param.data.mul_(alpha).add_(1 - alpha, param.data)

# def get_current_ent_weight(epoch):
#     # Consistency ramp-up from https://arxiv.org/abs/1610.02242
#     return 0.01 * sigmoid_rampup(epoch, 40)
thresold = 0.5


def sigmoid_rampup(current, rampup_length):
    """Exponential rampup from https://arxiv.org/abs/1610.02242"""
    if rampup_length == 0:
        return 1.0
    else:
        current = np.clip(current, 0.0, rampup_length)
        phase = 1.0 - current / rampup_length
        return float(np.exp(-5.0 * phase * phase))

# consistency_rampup = 200
consistency = 0.1
def get_current_consistency_weight(epoch):
    # Consistency ramp-up from https://arxiv.org/abs/1610.02242
    return consistency * sigmoid_rampup(epoch,40)

def val_casenet(epoch, model,model_resnet, data_loader, args, MODE):
    """
	:param epoch: current epoch number
	:param model: CNN model
	:param data_loader: evaluation and testing data
	:param args: global arguments args
	:param save_dir: save directory
	:param test_flag: current mode of validation or testing
	:return: performance evaluation of the current epoch
	"""
    model.eval()
    model_resnet.eval()
    starttime = time.time()
    case_name = None
    # case = []
    lossHist = []
    case_loss = []
    p_total = []
    sensitivity_total = []
    dice_hard_total = []
    # pre_total = []

    with torch.no_grad():
        for i, (x, y, coord, org, spac, NameID, SplitID, nzhw, ShapeOrg) in enumerate(tqdm(data_loader)):
            ######Wrap Tensor##########
            # NameID = NameID[0]
            NameID = list(set(NameID))
            if case_name == None:
                orgin = org[0]
                spacing = spac[0]
                zhw = nzhw[0]
                Shape = ShapeOrg[0]
                case_name = NameID[0]

                if hasattr(torch.cuda,'empty_cache'):
                    torch.cuda.empty_cache()

                p, loss = inference(x, y, coord, SplitID, orgin, spacing, zhw, Shape, args, model,model_resnet)
                p_total += p
                case_loss.append(loss)
            # case.append([x, y, coord, org, spac, NameID, SplitID, nzhw, ShapeOrg])
            elif case_name == NameID[0] and len(NameID) == 1:
                p, loss = inference(x, y, coord, SplitID, orgin, spacing, zhw, Shape, args, model,model_resnet)
                p_total += p
                case_loss.append(loss)

            elif case_name != NameID[0] and len(NameID) == 1:
                loss, sensiti, dice_hard = metric(p_total, case_loss, case_name, args, MODE)
                lossHist.append(loss)
                sensitivity_total.append(sensiti)
                dice_hard_total.append(dice_hard)
                p_total.clear()
                case_loss.clear()
                orgin = org[0]
                spacing = spac[0]
                zhw = nzhw[0]
                Shape = ShapeOrg[0]
                case_name = NameID[0]
                p, loss = inference(x, y, coord, SplitID, orgin, spacing, zhw, Shape, args, model,model_resnet)
                p_total += p
                case_loss.append(loss)

            elif len(NameID) == 2:
                if SplitID[1] == 0:
                    j = 1
                if SplitID[2] == 0:
                    j = 2
                if SplitID[3] == 0:
                    j = 3
                p, loss = inference(x[:j], y[:j], coord[:j], SplitID[:j], orgin, spacing, zhw, Shape, args, model,model_resnet)
                p_total += p
                case_loss.append(loss)
                loss, sensiti, dice_hard = metric(p_total, case_loss, case_name, args, MODE)
                lossHist.append(loss)
                sensitivity_total.append(sensiti)
                dice_hard_total.append(dice_hard)
                p_total.clear()
                case_loss.clear()
                NameID.remove(case_name)
                case_name = NameID[0]
                orgin = org[j]
                spacing = spac[j]
                zhw = nzhw[j]
                Shape = ShapeOrg[j]
                p, loss = inference(x[j:], y[j:], coord[j:], SplitID[j:], orgin, spacing, zhw, Shape, args, model,model_resnet)
                p_total += p
                case_loss.append(loss)
            # loss, sensiti, dice_hard, sp = inference(case, model, args, MODE)
            # lossHist.append(loss)
            # sensitivity_total.append(sensiti)
            # dice_hard_total.append(dice_hard)
            # sp_total.append(sp)
            # case.clear()
            # case_name = NameID
            # case.append([x, y, coord, org, spac, NameID, SplitID, nzhw, ShapeOrg])

            if i == (len(data_loader) - 1):
                # p, loss = inference(x, y, coord, SplitID, orgin, spacing, zhw, Shape, args, model)
                # p_total += p
                # case_loss.append(loss)
                loss, sensiti, dice_hard = metric(p_total, case_loss, case_name, args, MODE)
                lossHist.append(loss)
                sensitivity_total.append(sensiti)
                dice_hard_total.append(dice_hard)
                p_total.clear()
                case_loss.clear()
        endtime = time.time()
        mean_dice_hard = np.mean(np.array(dice_hard_total))
        mean_sensiti = np.mean(np.array(sensitivity_total))
        # mean_fpr = 1 - np.mean(np.array(sp_total))
        mean_loss = np.mean(np.array(lossHist))
        print('%s, epoch %d, loss %.4f, sensitivity %.4f, dice %.4f, time %3.2f'
              % ('val', epoch, mean_loss, mean_sensiti, mean_dice_hard,  endtime - starttime))
        print()
        gc.collect()
        empty_cache()
        return mean_loss, mean_sensiti, mean_dice_hard

def val_casenet_1(epoch, model, data_loader, args, MODE):
    """
	:param epoch: current epoch number
	:param model: CNN model
	:param data_loader: evaluation and testing data
	:param args: global arguments args
	:param save_dir: save directory
	:param test_flag: current mode of validation or testing
	:return: performance evaluation of the current epoch
	"""
    model.eval()
    #model_resnet.eval()
    starttime = time.time()
    case_name = None
    # case = []
    lossHist = []
    case_loss = []
    p_total = []
    sensitivity_total = []
    dice_hard_total = []
    # pre_total = []

    with torch.no_grad():
        for i, (x, y, coord, org, spac, NameID, SplitID, nzhw, ShapeOrg) in enumerate(tqdm(data_loader)):
            ######Wrap Tensor##########
            # NameID = NameID[0]
            NameID = list(set(NameID))
            if case_name == None:
                orgin = org[0]
                spacing = spac[0]
                zhw = nzhw[0]
                Shape = ShapeOrg[0]
                case_name = NameID[0]

                if hasattr(torch.cuda,'empty_cache'):
                    torch.cuda.empty_cache()

                p, loss = inference_1(x, y, coord, SplitID, orgin, spacing, zhw, Shape, args, model)
                p_total += p
                case_loss.append(loss)
            # case.append([x, y, coord, org, spac, NameID, SplitID, nzhw, ShapeOrg])
            elif case_name == NameID[0] and len(NameID) == 1:
                p, loss = inference_1(x, y, coord, SplitID, orgin, spacing, zhw, Shape, args, model)
                p_total += p
                case_loss.append(loss)

            elif case_name != NameID[0] and len(NameID) == 1:
                loss, sensiti, dice_hard = metric(p_total, case_loss, case_name, args, MODE)
                lossHist.append(loss)
                sensitivity_total.append(sensiti)
                dice_hard_total.append(dice_hard)
                p_total.clear()
                case_loss.clear()
                orgin = org[0]
                spacing = spac[0]
                zhw = nzhw[0]
                Shape = ShapeOrg[0]
                case_name = NameID[0]
                p, loss = inference_1(x, y, coord, SplitID, orgin, spacing, zhw, Shape, args, model)
                p_total += p
                case_loss.append(loss)

            elif len(NameID) == 2:
                if SplitID[1] == 0:
                    j = 1
                if SplitID[2] == 0:
                    j = 2
                if SplitID[3] == 0:
                    j = 3
                p, loss = inference_1(x[:j], y[:j], coord[:j], SplitID[:j], orgin, spacing, zhw, Shape, args, model)
                p_total += p
                case_loss.append(loss)
                loss, sensiti, dice_hard = metric(p_total, case_loss, case_name, args, MODE)
                lossHist.append(loss)
                sensitivity_total.append(sensiti)
                dice_hard_total.append(dice_hard)
                p_total.clear()
                case_loss.clear()
                NameID.remove(case_name)
                case_name = NameID[0]
                orgin = org[j]
                spacing = spac[j]
                zhw = nzhw[j]
                Shape = ShapeOrg[j]
                p, loss = inference_1(x[j:], y[j:], coord[j:], SplitID[j:], orgin, spacing, zhw, Shape, args, model)
                p_total += p
                case_loss.append(loss)
            # loss, sensiti, dice_hard, sp = inference(case, model, args, MODE)
            # lossHist.append(loss)
            # sensitivity_total.append(sensiti)
            # dice_hard_total.append(dice_hard)
            # sp_total.append(sp)
            # case.clear()
            # case_name = NameID
            # case.append([x, y, coord, org, spac, NameID, SplitID, nzhw, ShapeOrg])

            if i == (len(data_loader) - 1):
                # p, loss = inference(x, y, coord, SplitID, orgin, spacing, zhw, Shape, args, model)
                # p_total += p
                # case_loss.append(loss)
                loss, sensiti, dice_hard = metric(p_total, case_loss, case_name, args, MODE)
                lossHist.append(loss)
                sensitivity_total.append(sensiti)
                dice_hard_total.append(dice_hard)
                p_total.clear()
                case_loss.clear()
        endtime = time.time()
        mean_dice_hard = np.mean(np.array(dice_hard_total))
        mean_sensiti = np.mean(np.array(sensitivity_total))
        # mean_fpr = 1 - np.mean(np.array(sp_total))
        mean_loss = np.mean(np.array(lossHist))
        print('%s, epoch %d, loss %.4f, sensitivity %.4f, dice %.4f, time %3.2f'
              % ('val', epoch, mean_loss, mean_sensiti, mean_dice_hard,  (endtime - starttime)))
        gc.collect()
        empty_cache()
        return mean_loss, mean_sensiti, mean_dice_hard
def inference_1(x, y, coord, SplitID, orgin, spacing, zhw, Shape, args, model):
    p_total = []
    # lossHist =[]
    batchlen = x.size(0)
    x = x.cuda()
    model = model.cuda()
    #model_resnet = model_resnet.cuda()
    coord = coord.cuda()
    # casePreds, z = model(x, coord)
    with torch.no_grad():
        if hasattr(torch.cuda,'empty_cache'):
            torch.cuda.empty_cache()
        casePreds2 = model(x)
        # casePreds2 = model(x)[0]
        #casePreds3 = model_resnet(x)

    # if MODE == 'train' or MODE == 'semi_train':
    y = y.cuda()

    loss = torch.zeros(1)

    #####################seg data#######################
    outdata = casePreds2[0].cpu().data.numpy()
    outputs_soft_1 = F.softmax(casePreds2[0], dim=1)
    #outputs_soft_2 = F.softmax(casePreds3,dim=1)



    # origindata = org.numpy()
    # spacingdata = spac.numpy()
    outdata_1 = torch.argmax(outputs_soft_1, dim=1).cpu().data.numpy()
    outdata_1 = outdata_1[:, np.newaxis, :, :, :]
    #outdata_2 = torch.argmax(outputs_soft_2,dim=1).cpu().data.numpy()
    #outdata_2 = outdata_2[:,np.newaxis,:,:,:]

    #outdata2 = (outdata_1+outdata_2)/2

    for j in range(batchlen):
        segpred = outdata_1[j, 0]
        # curorigin = origindata[j].tolist()
        # curspacing = spacingdata[j].tolist()
        cursplitID = int(SplitID[j])
        assert (cursplitID >= 0)
        # curName = NameID
        # curnzhw = nzhw[j]
        # curshape = ShapeOrg[j]

        curpinfo = [segpred, cursplitID, zhw, Shape, orgin, spacing]
        p_total.append(curpinfo)

    return p_total, loss.item() / batchlen

def inference(x, y, coord, SplitID, orgin, spacing, zhw, Shape, args, model,model_resnet):
    p_total = []
    # lossHist =[]
    batchlen = x.size(0)
    x = x.cuda()
    model = model.cuda()
    model_resnet = model_resnet.cuda()
    coord = coord.cuda()
    # casePreds, z = model(x, coord)
    with torch.no_grad():
        if hasattr(torch.cuda,'empty_cache'):
            torch.cuda.empty_cache()
        casePreds2 = model(x)
        #casePreds2 = casePreds2[0]
        casePreds3 = model_resnet(x)

    # if MODE == 'train' or MODE == 'semi_train':
    y = y.cuda()

    loss = torch.zeros(1)
    # for evaluation
    # lossHist.append(loss.item())

    #####################seg data#######################
    #outdata = casePreds2.cpu().data.numpy()
    outputs_soft_1 = F.softmax(casePreds2[0], dim=1)
    outputs_soft_2 = F.softmax(casePreds3[0],dim=1)

    # origindata = org.numpy()
    # spacingdata = spac.numpy()
    #outputs_sum = (outputs_soft_1+outputs_soft_2)/2
    #outdata_2 = torch.argmax(outputs_sum,dim=1).cpu().data.numpy()
    #outdata_2 = outdata_2[:,np.newaxis,:,:,:]

    outdata_1 = torch.argmax(outputs_soft_1, dim=1).cpu().data.numpy()
    outdata_1 = outdata_1[:, np.newaxis, :, :, :]
    outdata_2 = torch.argmax(outputs_soft_2,dim=1).cpu().data.numpy()
    outdata_2 = outdata_2[:,np.newaxis,:,:,:]
    outdata_2=(outdata_2+outdata_1)/2



    for j in range(batchlen):
        segpred = outdata_2[j, 0]
        # curorigin = origindata[j].tolist()
        # curspacing = spacingdata[j].tolist()
        cursplitID = int(SplitID[j])
        assert (cursplitID >= 0)
        # curName = NameID
        # curnzhw = nzhw[j]
        # curshape = ShapeOrg[j]

        curpinfo = [segpred, cursplitID, zhw, Shape, orgin, spacing]
        p_total.append(curpinfo)

    return p_total, loss.item() / batchlen


def metric(p_total, lossHist, case_name, args, MODE):
    curName = case_name
    # curp = p_total[curName]
    # y_combine, curorigin, curspacing = combine_total(cury, sidelen, margin)
    sidelen = args.stridev
    if args.cubesizev is not None:
        margin = args.cubesizev
    else:
        margin = args.cubesize
    p_combine, porigin, pspacing = combine_total_avg(p_total, sidelen, margin)
    p_combine_bw = (p_combine > th_bin)

    # 关键在这里val 和test 区别 你看完可以注释掉
    if MODE == 'val':

        label_file = curName
        # label_file = os.path.join(preprocessed_data_dir, curName + '_label.nii.gz')
        # print(f"val_label_file{label_file}")
        assert (os.path.exists(label_file) is True)
        y_combine, curorigin, curspacing= load_itk_image(label_file)

        curdicehard = dice_coef_np(p_combine_bw, y_combine)
        cursensi = sensitivity_np(p_combine_bw, y_combine)
        # hd =
        ########################################################################
        # name_total.append(curName)
        lossHist = np.array(lossHist)
        mean_loss = np.mean(lossHist)
        del p_combine_bw, p_combine
        p_total.clear()
        gc.collect()
        empty_cache()
        return mean_loss, cursensi, curdicehard

    if MODE == 'test':
        label_file = curName
        # print(label_file)
        assert (os.path.exists(label_file) is True)
        y_combine, curorigin, curspacing = load_itk_image(label_file)

        curdicehard = dice_coef_np(p_combine_bw, y_combine)
        cursensi = sensitivity_np(p_combine_bw, y_combine)
        # sp = FPR_np(p_combine_bw, y_combine)
        #生真实和预测文件的路径
        # curName  = curName.split("\\")[-1]
        # print(f"args.test_dir{args.test_dir}")
        # xx = args.test_dir
        print(curName[-10:-7])
        xx = curName[-10:-7]
        curypath = os.path.join(args.test_dir, '%s-case-gt.nii.gz' % (xx))
        # assert (os.path.exists(curypath) is True)
        curpredpath = os.path.join(args.test_dir, '%s-case-pred.nii.gz' % (xx))
        # save_itk(x_combine.astype(dtype='uint8'), curorigin, curspacing, curpath)
        # 生成真实和预测文件
        save_itk(y_combine.astype(dtype='uint8'), curorigin, curspacing, curypath)
        save_itk(p_combine_bw.astype(dtype='uint8'), porigin, pspacing, curpredpath)

        ########################################################################
        # name_total.append(curName)
        lossHist = np.array(lossHist)
        mean_loss = np.mean(lossHist)
        del p_combine_bw, p_combine
        p_total.clear()
        gc.collect()
        empty_cache()
        return mean_loss, cursensi, curdicehard

    if MODE == 'pseudo':
        # multiply lung mask
        lung_mask_file = os.path.join(args.lung_mask_dir, curName + '_lung_mask.nii.gz')
        assert (os.path.exists(lung_mask_file) is True)
        lung_mask, _, _ = load_itk_image(lung_mask_file)
        p_combine_bw = p_combine_bw * lung_mask

        curdicehard = 0
        cursensi = 0
        sp = 0

        # curypath = os.path.join(args.test_dir, '%s-case-gt.nii.gz'%(curName))
        curpredpath = os.path.join(args.test_dir, '%s_label.nii.gz' % (curName))

        # save_itk(y_combine.astype(dtype='uint8'), curorigin, curspacing, curypath)
        save_itk(p_combine_bw.astype(dtype='uint8'), porigin, pspacing, curpredpath)

        ########################################################################
        # name_total.append(curName)
        lossHist = np.array(lossHist)
        mean_loss = np.mean(lossHist)
        del p_combine_bw, p_combine
        p_total.clear()
        gc.collect()
        empty_cache()
        return mean_loss, cursensi, curdicehard


# def test_casenet(epoch, model, data_loader, args, save_dir, lung_mask_dir, test_flag=False):
#     """
# 	:param epoch: current epoch number
# 	:param model: CNN model
# 	:param data_loader: evaluation and testing data
# 	:param args: global arguments args
# 	:param save_dir: save directory
# 	:param test_flag: current mode of validation or testing
# 	:return: performance evaluation of the current epoch
# 	"""
#     model.eval()
#     starttime = time.time()
#
#     sidelen = args.stridev
#     if args.cubesizev is not None:
#         margin = args.cubesizev
#     else:
#         margin = args.cubesize
#
#     name_total = []
#     sensitivity_total = []
#     dice_hard_total = []
#     specificity_total = []
#
#     valdir = os.path.join(save_dir, 'test%03d' % (epoch))
#     state_str = 'test'
#     if not os.path.exists(valdir):
#         os.mkdir(valdir)
#
#     p_total = {}
#     x_total = {}
#
#     with torch.no_grad():
#         for i, (x, y, coord, org, spac, NameID, SplitID, nzhw, ShapeOrg) in enumerate(tqdm(data_loader)):
#             ######Wrap Tensor##########
#             # NameID = NameID[0]
#             # SplitID = SplitID[0]
#             batchlen = x.size(0)
#             x = x.cuda()
#             y = y.cuda()
#             ####################################################
#             coord = coord.cuda()
#             casePreds = model(x, coord)
#
#             # if args.deepsupervision:
#             # 	weights = [0.53333333, 0.26666667, 0.13333333, 0.06666667, 0.0]
#             # 	loss = weights[0] * loss_function(casePreds[0], y[0])
#             # 	for i in range(1, len(weights)):
#             # 		if weights[i] != 0:
#             # 			loss += weights[i] * loss_function(casePreds[i], y[i])
#             # else:
#             # 	loss = loss_function(casePreds[0], y[0])
#             # loss += binary_cross_entropy(casePred, y)
#             # loss += focal_loss(casePred, y)
#
#             # for evaluation
#             # lossHist.append(loss.item())
#
#             #####################seg data#######################
#             outdata = casePreds[0].cpu().data.numpy()
#             # segdata = y.cpu().data.numpy()
#             # segdata = (segdata > th_bin)
#             origindata = org.numpy()
#             spacingdata = spac.numpy()
#             #######################################################################
#             #################REARRANGE THE DATA BY SPLIT ID########################
#             for j in range(batchlen):
#                 # curydata = segdata[j, 0]
#                 segpred = outdata[j, 0]
#                 curorigin = origindata[j].tolist()
#                 curspacing = spacingdata[j].tolist()
#                 cursplitID = int(SplitID[j])
#                 assert (cursplitID >= 0)
#                 curName = NameID[j]
#                 curnzhw = nzhw[j]
#                 curshape = ShapeOrg[j]
#
#                 if not (curName in x_total.keys()):
#                     x_total[curName] = []
#                 # if not (curName in y_total.keys()):
#                 # 	y_total[curName] = []
#                 if not (curName in p_total.keys()):
#                     p_total[curName] = []
#
#                 # curxinfo = [curxdata, cursplitID, curnzhw, curshape, curorigin, curspacing]
#                 # curyinfo = [curydata, cursplitID, curnzhw, curshape, curorigin, curspacing]
#                 curpinfo = [segpred, cursplitID, curnzhw, curshape, curorigin, curspacing]
#                 # x_total[curName].append(curxinfo)
#                 # y_total[curName].append(curyinfo)
#                 p_total[curName].append(curpinfo)
#
#     # combine all the cases together
#     for curName in x_total.keys():
#         # curx = x_total[curName]
#         # cury = y_total[curName]
#         curp = p_total[curName]
#         # x_combine, xorigin, xspacing = combine_total(curx, sidelen, margin)
#         # y_combine, curorigin, curspacing = combine_total(cury, sidelen, margin)
#         label_file = os.path.join(args.preprocessed_data, curName + '_label.nii.gz')
#         assert (os.path.exists(label_file) is True)
#         y_combine, _, _ = load_itk_image(label_file)
#
#         p_combine, porigin, pspacing = combine_total_avg(curp, sidelen, margin)
#         p_combine_bw = (p_combine > th_bin)
#         # multiply lung mask
#         lung_mask_file = os.path.join(lung_mask_dir, curName + '_lung_mask.nii.gz')
#         assert (os.path.exists(lung_mask_file) is True)
#         lung_mask, _, _ = load_itk_image(lung_mask_file)
#         p_combine_bw = p_combine_bw * lung_mask
#         # curpath = os.path.join(valdir, '%s-case-org.nii.gz'%(curName))
#         curypath = os.path.join(valdir, '%s-case-gt.nii.gz' % (curName))
#         curpredpath = os.path.join(valdir, '%s-case-pred.nii.gz' % (curName))
#         # save_itk(x_combine.astype(dtype='uint8'), curorigin, curspacing, curpath)
#         save_itk(y_combine.astype(dtype='uint8'), curorigin, curspacing, curypath)
#         save_itk(p_combine_bw.astype(dtype='uint8'), curorigin, curspacing, curpredpath)
#
#         ########################################################################
#         curdicehard = dice_coef_np(p_combine_bw, y_combine)
#         cursensi = sensitivity_np(p_combine_bw, y_combine)
#         cursp = specificity_np(p_combine_bw.astype(dtype='uint8'), y_combine.astype(dtype='uint8'))
#         ########################################################################
#         name_total.append(curName)
#         sensitivity_total.append(cursensi)
#         dice_hard_total.append(curdicehard)
#         specificity_total.append(cursp)
#         del curp, y_combine, p_combine_bw, p_combine
#
#     endtime = time.time()
#     all_results = {'lidc': [], 'exact09': []}
#
#     with open(os.path.join(valdir, 'val_results.csv'), 'w') as csvout:
#         writer = csv.writer(csvout)
#         row = ['name', 'val dice', 'val sensi', 'val FPR']
#         writer.writerow(row)
#
#         for i in range(len(name_total)):
#             name = name_total[i]
#             if name[0] == 'L':
#                 keyw = 'lidc'
#             elif name[0] == 'C':
#                 keyw = 'exact09'
#
#             row = [name_total[i], float(round(dice_hard_total[i] * 100, 3)),
#                    float(round(sensitivity_total[i] * 100, 3)),
#                    float(round((1 - specificity_total[i]) * 100, 6))]
#             all_results[keyw].append([float(row[1]), float(row[2]), float(row[3])])
#             writer.writerow(row)
#
#         lidc_results = np.mean(np.array(all_results['lidc']), axis=0)
#         exact09_results = np.mean(np.array(all_results['exact09']), axis=0)
#
#         lidc_results2 = np.std(np.array(all_results['lidc']), axis=0)
#         exact09_results2 = np.std(np.array(all_results['exact09']), axis=0)
#
#         all_res_mean = np.mean(np.array(all_results['lidc'] + all_results['exact09']), axis=0)
#         all_res_std = np.std(np.array(all_results['lidc'] + all_results['exact09']), axis=0)
#         # yyy change
#         lidc_mean = ['lidc mean', lidc_results[0], lidc_results[1], lidc_results[2]]
#         lidc_std = ['lidc std', lidc_results2[0], lidc_results2[1], lidc_results2[2]]
#
#         ex_mean = ['exact09 mean', exact09_results[0], exact09_results[1], exact09_results[2]]
#         ex_std = ['exact09 std', exact09_results2[0], exact09_results2[1], exact09_results2[2]]
#
#         all_mean = ['all mean', all_res_mean[0], all_res_mean[1], all_res_mean[2]]
#         all_std = ['all std', all_res_std[0], all_res_std[1], all_res_std[2]]
#
#         # yyy change
#         writer.writerow(lidc_mean)
#         writer.writerow(lidc_std)
#         writer.writerow(ex_mean)
#         writer.writerow(ex_std)
#         writer.writerow(all_mean)
#         writer.writerow(all_std)
#         csvout.close()
#
#     mean_dice_hard = np.mean(np.array(dice_hard_total)) * 100
#     mean_sensiti = np.mean(np.array(sensitivity_total)) * 100
#     mean_SP = (1 - np.mean(np.array(specificity_total))) * 100
#     print('%s, epoch %d, sensitivity %.4f, dice %.4f, fpr %.8f, time %3.2f'
#           % (state_str, epoch, mean_sensiti, mean_dice_hard, mean_SP, endtime - starttime))
#     print()
#     empty_cache()
#     return mean_sensiti, mean_dice_hard
