# DeepLearnUtils 
# Copyright (c) 2017-8 Eric Kerfoot, KCL, see LICENSE file

from __future__ import print_function,division
from collections import OrderedDict
import numpy as np
import torch
import torch.nn as nn
from torch.nn.modules.loss import _Loss


def samePadding(kernelsize):
    if isinstance(kernelsize,tuple):
        return tuple((k-1)//2 for k in kernelsize)
    else:
        return (kernelsize-1)//2
    

class BinaryDiceLoss(_Loss):
    def forward(self, source, target, smooth=1e-5):
        batchsize = target.size(0)
        probs = source.float().sigmoid()
        psum = probs.view(batchsize, -1)
        tsum = target.float().view(batchsize, -1)
        
        intersection=psum*tsum
        sums=psum+tsum

        score = 2.0 * (intersection.sum(1) + smooth) / (sums.sum(1) + smooth)
        score = 1 - score.sum() / batchsize
        return score
    

class Convolution2D(nn.Module):
    def __init__(self,inChannels,outChannels,strides=1,kernelsize=3,instanceNorm=True,dropout=0):
        super(Convolution2D,self).__init__()
        self.inChannels=inChannels
        self.outChannels=outChannels
        padding=samePadding(kernelsize)
        normalizeFunc=nn.InstanceNorm2d if instanceNorm else nn.BatchNorm2d
        
        self.conv=nn.Sequential(
            nn.Conv2d(inChannels,outChannels,kernel_size=kernelsize,stride=strides,padding=padding),
            normalizeFunc(outChannels),#,track_running_stats=True), 
            nn.Dropout2d(dropout),
            nn.modules.PReLU()
        )
        
    def forward(self,x):
        return self.conv(x)
        

class ResidualUnit2D(nn.Module):
    def __init__(self, inChannels,outChannels,strides=1,kernelsize=3,subunits=2,instanceNorm=True,dropout=0):
        super(ResidualUnit2D,self).__init__()
        self.inChannels=inChannels
        self.outChannels=outChannels
        
        padding=samePadding(kernelsize)
        seq=[]
        schannels=inChannels
        sstrides=strides
        
        for su in range(subunits):
            seq.append(Convolution2D(schannels,outChannels,sstrides,kernelsize,instanceNorm,dropout))
            schannels=outChannels # after first loop set the channels and strides to what they should be for subsequent units
            sstrides=1
            
        self.conv=nn.Sequential(*seq) # apply this sequence of operations to the input
        
        # apply this convolution to the input to change the number of output channels and output size to match that coming from self.conv
        self.residual=nn.Conv2d(inChannels,outChannels,kernel_size=kernelsize,stride=strides,padding=padding)
        
    def forward(self,x):
        res=self.residual(x) # create the additive residual from x
        cx=self.conv(x) # apply x to sequence of operations
        
        return cx+res # add the residual to the output
    

class ResidualBranchUnit2D(nn.Module):
    def __init__(self, inChannels,outChannels,strides=1,branches=[(3,)],instanceNorm=True,dropout=0):
        super(ResidualBranchUnit2D,self).__init__()
        self.inChannels=inChannels
        self.outChannels=outChannels
        self.branchSeqs=[]
        
        totalchannels=0
        for i,branch in enumerate(branches):
            seq=[]
            sstrides=strides
            schannels=inChannels
            ochannels=max(1,outChannels//len(branches))
            totalchannels+=ochannels
            
            for kernel in branch:
                seq.append(Convolution2D(schannels,ochannels,sstrides,kernel,instanceNorm,dropout))
                schannels=ochannels # after first conv set the channels and strides to what they should be for subsequent units
                sstrides=1
                
            seq=nn.Sequential(*seq)
            setattr(self,'branch%i'%i,seq)
            self.branchSeqs.append(seq)
            
        # resize branches to have the desired number of output channels
        self.resizeconv=nn.Conv2d(totalchannels,outChannels,kernel_size=1,stride=1)
        
        # apply this convolution to the input to change the number of output channels and output size to match that coming from self.conv
        self.residual=nn.Conv2d(inChannels,outChannels,kernel_size=3,stride=strides,padding=samePadding(3))
        
    def forward(self,x):
        res=self.residual(x) # create the additive residual from x
        
        cx=torch.cat([s(x) for s in self.branchSeqs],1)
        cx=self.resizeconv(cx)
        
        return cx+res # add the residual to the output
    

class UpsampleConcat2D(nn.Module):
    def __init__(self,inChannels,outChannels,strides=1,kernelsize=3):
        super(UpsampleConcat2D,self).__init__()
        padding=strides-1
        self.convt=nn.ConvTranspose2d(inChannels,outChannels,kernelsize,strides,1,padding)
      
    def forward(self,x,y):
        x=self.convt(x)
        return torch.cat([x,y],1)


class ResidualClassifier(nn.Module):
    def __init__(self,inShape,classes,channels,strides,kernelsize=3,numSubunits=2,instanceNorm=True,dropout=0):
        super(ResidualClassifier,self).__init__()
        assert len(channels)==len(strides)
        self.inHeight,self.inWidth,self.inChannels=inShape
        self.channels=channels
        self.strides=strides
        self.classes=classes
        self.kernelsize=kernelsize
        self.numSubunits=numSubunits
        self.instanceNorm=instanceNorm
        self.dropout=dropout
        
        modules=[]
        self.linear=None
        echannel=self.inChannels
        
        self.finalSize=np.asarray([self.inHeight,self.inWidth],np.int)
        
        # encode stage
        for i,(c,s) in enumerate(zip(self.channels,self.strides)):
            modules.append(('layer_%i'%i,ResidualUnit2D(echannel,c,s,self.kernelsize,self.numSubunits,instanceNorm,dropout)))
            
            echannel=c # use the output channel number as the input for the next loop
            self.finalSize=self.finalSize//s

        self.linear=nn.Linear(int(np.product(self.finalSize))*echannel,self.classes)
        
        self.classifier=nn.Sequential(OrderedDict(modules))
        
    def forward(self,x):
        b=x.size(0)
        x=self.classifier(x)
        x=x.view(b,-1)
        x=self.linear(x)
        return (x,)
        

class BranchClassifier(nn.Module):
    def __init__(self,inShape,classes,channels,strides,branches=[(3,)],instanceNorm=True,dropout=0):
        super(BranchClassifier,self).__init__()
        assert len(channels)==len(strides)
        self.inHeight,self.inWidth,self.inChannels=inShape
        self.channels=channels
        self.strides=strides
        self.classes=classes
        self.branches=branches
        self.instanceNorm=instanceNorm
        self.dropout=dropout
        
        modules=[]
        self.linear=None
        echannel=self.inChannels
        
        self.finalSize=np.asarray([self.inHeight,self.inWidth],np.int)
        
        # encode stage
        for i,(c,s) in enumerate(zip(self.channels,self.strides)):
            modules.append(('layer_%i'%i,ResidualBranchUnit2D(echannel,c,s,self.branches,instanceNorm,dropout)))
            
            echannel=c*len(branches) # use the output channel number as the input for the next loop
            self.finalSize=self.finalSize//s

        self.linear=nn.Linear(int(np.product(self.finalSize))*echannel,self.classes)
        
        self.classifier=nn.Sequential(OrderedDict(modules))
        
    def forward(self,x):
        b=x.size(0)
        x=self.classifier(x)
        x=x.view(b,-1)
        x=self.linear(x)
        return (x,)
    

class AutoEncoder2D(nn.Module):
    def __init__(self,inChannels,outChannels,channels,strides,kernelsize=3,numSubunits=2,instanceNorm=True,dropout=0):
        super(AutoEncoder2D,self).__init__()
        assert len(channels)==len(strides)
        self.inChannels=inChannels
        self.outChannels=outChannels
        self.channels=channels
        self.strides=strides
        self.kernelsize=kernelsize
        self.numSubunits=numSubunits
        self.instanceNorm=instanceNorm
        
        modules=[]
        echannel=inChannels
        
        # encoding stage
        for i,(c,s) in enumerate(zip(channels,strides)):
            modules.append(('encode_%i'%i,ResidualUnit2D(echannel,c,s,self.kernelsize,self.numSubunits,instanceNorm,dropout)))
            echannel=c
            
        # decoding stage
        for i,(c,s) in enumerate(zip(list(channels[-2::-1])+[outChannels],strides[::-1])):
            modules+=[
                ('up_%i'%i,nn.ConvTranspose2d(echannel,echannel,self.kernelsize,s,1,s-1)),
                ('decode_%i'%i,ResidualUnit2D(echannel,c,1,self.kernelsize,self.numSubunits,instanceNorm,dropout))
            ]
            echannel=c

        self.conv=nn.Sequential(OrderedDict(modules))
        
    def forward(self,x):
        return (self.conv(x),)
    
    
class BaseUnet2D(nn.Module):
    def __init__(self,inChannels,numClasses,channels,strides,upsampleKernelSize=3):
        super(BaseUnet2D,self).__init__()
        assert len(channels)==len(strides)
        self.inChannels=inChannels
        self.numClasses=numClasses
        self.channels=channels
        self.strides=strides
        self.upsampleKernelSize=upsampleKernelSize
        
        dchannels=[self.numClasses]+list(self.channels[:-1])

        self.encodes=[] # list of encode stages, this is build up in reverse order so that the decode stage works in reverse
        self.decodes=[]
        echannel=inChannels
        
        # encode stage
        for c,s,dc in zip(self.channels,self.strides,dchannels):
            ex=self._getLayer(echannel,c,s,True)
            
            setattr(self,'encode_%i'%(len(self.encodes)),ex)
            self.encodes.insert(0,(ex,dc,s,echannel))
            echannel=c # use the output channel number as the input for the next loop
            
        # decode stage
        for ex,c,s,ec in self.encodes:
            up=self._getUpsampleConcat(echannel,echannel,s,self.upsampleKernelSize)
            x=self._getLayer(echannel+ec,c,1,False)
            echannel=c
            
            setattr(self,'up_%i'%(len(self.decodes)),up)
            setattr(self,'decode_%i'%(len(self.decodes)),x)
            
            self.decodes.append((up,x))
            
    def _getUpsampleConcat(self,inChannels,outChannels,stride,kernelSize):
        pass
    
    def _getLayer(self,inChannels,outChannels,strides,isEncode):
        pass
        
    def forward(self,x):
        elist=[] # list of encode stages, this is build up in reverse order so that the decode stage works in reverse

        # encode stage
        for ex,_,_,_ in reversed(self.encodes):
            i=len(elist)
            addx=x
            x=ex(x)
            elist.insert(0,(addx,)+self.decodes[-i-1])

        # decode stage
        for addx,up,ex in elist:
            x=up(x,addx)
            x=ex(x)
            
        # generate prediction outputs, x has shape BCHW
        if self.numClasses==1:
            preds=(x[:,0]>=0.5).type(torch.IntTensor)
        else:
            preds=x.max(1)[1] # take the index of the max value along dimension 1

        return x, preds


class Unet2D(BaseUnet2D):
    def __init__(self,inChannels,numClasses,channels,strides,kernelsize=3,numSubunits=2,instanceNorm=True,dropout=0):
         self.kernelsize=kernelsize
         self.numSubunits=numSubunits
         self.instanceNorm=instanceNorm
         self.dropout=dropout
         super(Unet2D,self).__init__(inChannels,numClasses,channels,strides,3)

    def _getUpsampleConcat(self,inChannels,outChannels,stride,kernelSize):
        return UpsampleConcat2D(inChannels,outChannels,stride,kernelSize)    
     
    def _getLayer(self,inChannels,outChannels,strides,isEncode):
        return ResidualUnit2D(inChannels,outChannels,strides,self.kernelsize,self.numSubunits if isEncode else 1,self.instanceNorm,self.dropout)
    

class BranchUnet2D(BaseUnet2D):
    def __init__(self,inChannels,numClasses,channels,strides,branches,instanceNorm=True,dropout=0):
         self.branches=branches
         self.instanceNorm=instanceNorm
         self.dropout=dropout
         super(BranchUnet2D,self).__init__(inChannels,numClasses,channels,strides,3)

    def _getUpsampleConcat(self,inChannels,outChannels,stride,kernelSize):
        return UpsampleConcat2D(inChannels,outChannels,stride,kernelSize)
         
    def _getLayer(self,inChannels,outChannels,strides,isEncode):
        return ResidualBranchUnit2D(inChannels,outChannels,strides,self.branches,self.instanceNorm,self.dropout)
    