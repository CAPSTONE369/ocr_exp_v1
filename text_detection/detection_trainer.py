import torch
import torch.nn as nn
import torch.nn.functional as F
import cv2
from tqdm import tqdm
import os, sys
import datetime
import numpy as np
import mlflow
import random
import torchvision.transforms as transforms
import detect_model as DetectModel ## 모델 이름과 model_configuration을 입력으로 넣어줌
import losses as DetectLoss ## 손싷함수의 이름과 각각의 가중치를 위해서 train_configuration을 입력으로 넣어줌
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))) 
from tools.base_trainer import BaseTrainer
from loguru import logger
from torchmetrics.functional import precision_recall
from torchmetrics import AveragePrecision
from torchmetrics.detection.mean_ap import MeanAveragePrecision
from torchmetrics.functional import f1_score as F1
from utils.east_utils import east_detect
from utils.ctpn_utils import ctpn_detect

ARTIFACT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
## DETECTION TRAINER ##

optimizer_registry = {
    "ADAM": torch.optim.Adam,
    "SGD": torch.optim.SGD,
}

## 만약에 Adam optimizer을 사용한다면 learning rate에 momentum이 적용이 되어서 굳이 
# learning rate scheduler이 필요하지 않을 수도 있다.
scheduler_registry = {
    "MULTISTEP": torch.optim.lr_scheduler.MultiStepLR,
    "LINEAR": torch.optim.lr_scheduler.LinearLR,
    "COSINE": torch.optim.lr_scheduler.CosineAnnealingLR
}
now = datetime.datetime.now()
TODAY = str(now.strftime('%Y-%m-%d %H:%M:%S'))
class Trainer(BaseTrainer):
    def build(self):
        self.IMPROVED = False
        self.experiment_number = TODAY
        self.current_metric = {}
        logger.info(
            f"ARTIFACT DIR: {ARTIFACT_DIR}"
        )
        self.eval_epoch = self.train_cfg['eval_epoch']
        self.total_epochs = self.train_cfg['epoch'] + self.train_cfg['eval_epoch']
        ## build the model, optimizer, schduler, loss functions, etc ..
        # mlflow.log_artifacts(os.path.join(ARTIFACT_DIR, 'text_detection'), artifact_path = "code")
        self.model = DetectModel.load_model(self.model_cfg['model_name'], self.model_cfg).cuda()
        
        ## (1) LOAD THE PRETRAINED MODEL WEIGHTS
        ## --> CHANGED TO BE DONE IN THE LOAD_MODEL FILE
        """
        if self.model_cfg['pretrained_model'] != '':
            pretrained = torch.load(self.model_cfg['pretrained_model'])
            if 'model_state_dict' in pretrained:
                pretrained = pretrained['model_state_dict']
            self.model.load_state_dict(pretrained)
            # org = self.model.state_dict()
            #new = {key:value for key, value in pretrained.items() if key in org and \
                            #value.shape == pretrained[key].shape}
            #org.update(new)
            #self.model.load_state_dict(org)
        """
        if self.model_cfg['model_name'].upper() == 'EAST':
            self.model.extractor.eval()
            
        self.criterion, self.lamda = DetectLoss.load_loss(self.train_cfg) ## 모델별로 지정된 손실 함수를 불로오기 위해서 사용
        
        self.optimizer = optimizer_registry[self.train_cfg['optimizer'].upper()](
            params = self.model.parameters(), lr = self.train_cfg['learning_rate']
        )
        ## 당분간은 scheduler은 사용하지 않기로 하자
        
        self.best_metric_dict = {}
        self.current_metric_dict = {}

    def run(self, train_dataloader, eval_dataloader):
        self.build()
        logger.info("===> connected to detection trainer ===>")
        self.train_dataloader = train_dataloader
        self.eval_dataloader = eval_dataloader
        self.losses = {}
        self.model.train()
        for epoch in range(self.total_epochs):
    
            self.IMPROVED = False ## 매번 새롭게 업데이트
            if epoch == 0:
                # self.validate()
                self.save(first = True)
                #mlflow.log_metrics({'IoU': IOU})
                logger.info("FIRST EVALUATION TO CHECK IF ALL IS OK......")
            epoch_loss = 0.0
            REG_Loss = 0.0
            CLS_Loss = 0.0
            loss = 0.0
            train_loop = tqdm(self.train_dataloader)
            
            self.model.train()
            for idx, batch in enumerate(train_loop):
               
                if self.model_cfg['model_name'].upper() == 'EAST':
                    img, gt_score, gt_geo, gt_ignore = batch
                    
                    img, gt_score, gt_geo, gt_ignore = img.cuda(), gt_score.cuda(), gt_geo.cuda(), gt_ignore.cuda()
                    pred_score, pred_geo = self.model(img) 
                    ## (B, 1, W, H) (B, 5, W, H)
                    loss = self.criterion[0](gt_score, pred_score, gt_geo, pred_geo, gt_ignore)
                elif self.model_cfg['model_name'].upper() == 'CTPN':
                    # img, cls, regr = batch
                    img,targets, full_box = batch
                    img = img.cuda()
                    ground_truths = (targets[0].cuda(), targets[1].cuda())
        
                    pred_cls, pred_regr = self.model(img)
                    regr_loss, cls_loss  = self.criterion[0](pred_cls, pred_regr, ground_truths)
                    loss = regr_loss + cls_loss
                    
              
                epoch_loss += loss.item()
                REG_Loss += regr_loss.item()
                CLS_Loss += cls_loss.item()
                if loss.item() == 0:
                    continue
                else:
                    self.optimizer.zero_grad()
                    loss.backward()
                    self.optimizer.step()
                if self.model_cfg['model_name'].upper() == 'CTPN':
                    train_loop.set_postfix({
                        "Loss": loss.item(),
                        "Epoch": epoch,
                        "CLS LOSS": cls_loss.item(),
                        "REGR LOSS": regr_loss.item()
                    })
                else:
                    train_loop.set_postfix({
                        "Loss": loss.item(),
                        "Epoch": epoch,
                        "SCORE L1 Loss": F.l1_loss(input = gt_score, target = pred_score).item(),
                        "GEO L1 Loss": F.l1_loss(input = gt_geo, target = pred_geo).item()
                    })
                    
                    
                
                
            self.losses[epoch] = epoch_loss 
            mlflow.log_metrics({"LOSS": epoch_loss / len(train_loop)})
            mlflow.log_metrics({"REGRESSION LOSS": REG_Loss / len(train_loop)})
            mlflow.log_metrics({"CLASSIFICATION LOSS": CLS_Loss / len(train_loop)})
            self.save(last = False, first = False, epoch = epoch)
            if (epoch+1) % self.eval_epoch == 0:
                self.validate()
                self.save(last = False) ## 만약에 metric값, 즉 정확도가 향상이 되었다면 
                
        self.validate()
        self.save(last = True)            
        return
    
    
    def start_first_epoch(self, current_epoch):
        pass
    
    def save(self,  last = False, first = False, epoch = 0):
        '''
        Function for saving the model weights if best model or if it is the last epoch
        '''
        os.makedirs(os.path.join(self.train_cfg['eval_weight'], self.experiment_number), exist_ok = True)
        if first:
            new_path = os.path.join(self.train_cfg['eval_weight'], self.experiment_number, 'first.pt')
            torch.save(self.model.state_dict(), new_path)
        elif last:
            new_path = os.path.join(self.train_cfg['eval_weight'], self.experiment_number, 'last.pt')
            torch.save(self.model.state_dict(), new_path)
        else:
            if self.IMPROVED == True:
                new_path = os.path.join(self.train_cfg['eval_weight'], self.experiment_number, 'best.pt')
                torch.save(self.model.state_dict(), new_path)
                # client = mlflow.tracking.MlflowClient()
                # client.log_artifact()
                logger.info("SAVED BEST WEIGHT...")
            else:
                new_path = os.path.join(self.train_cfg['eval_weight'], self.experiment_number, f'EPOCH{epoch}.pt')
                torch.save(self.model.state_dict(), new_path)
                logger.info(f"SAVED WEIGHT OF EPOCH {epoch}....")

    def compare(self, new_dict, prev_dict):
        new = max(list(new_dict.values()))
        if prev_dict is None or prev_dict == {}:
            prev_dict = new_dict
            self.IMPROVED = True
            return
        prev = max(list(prev_dict.values()))
        if new > prev:
            self.IMPROVED = True
        else:
            self.IMPROVED = False

    def validate(self, root_path= './results'):
        ## evaluate 단계에서는 전체 이미지를 crop이나 height adjust없이 넣어준다.

        self.model.eval()
        
        idxs = random.sample(range(1, len(self.eval_dataloader)), 5)
        with torch.no_grad():
            loop = tqdm(self.eval_dataloader)
            if self.model_cfg['model_name'].upper() == 'EAST':
                for idx, batch in enumerate(loop):
                    pred_score = east_detect.detect_while_training(batch, self.model, score_thresh = 0.7, nms_thresh = 0.2)
                    # self.compare(pred_score, self.current_metric_dict)
                    
                    self.current_metric_dict = pred_score
                    loop.set_postfix(self.current_metric_dict)
                    
                    
            elif self.model_cfg['model_name'].upper() == 'CTPN':
                pred_score = ctpn_detect.detect_all(self.eval_dataloader, self.model, prob_thresh=0.3, iou=True)
                self.compare(pred_score, self.current_metric_dict)
                self.current_metric_dict = pred_score ## IOU score
                loop.set_postfix(self.current_metric_dict)
            


        