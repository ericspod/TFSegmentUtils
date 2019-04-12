# DeepLearnUtils 
# Copyright (c) 2017-8 Eric Kerfoot, KCL, see LICENSE file

from __future__ import division, print_function
from functools import partial,wraps
import numpy as np
import scipy.ndimage
import scipy.fftpack as ft

import trainutils
        

def augment(prob=0.5,applyIndices=None):
    '''
    Creates an augmentation function when applied to a function returning an array modifying callable. The function this
    is applied to is given the list of input arrays as positional arguments and then should return a callable operation
    which performs the augmentation. This wrapper then chooses whether to apply the operation to the arguments and if so
    to which ones. The `prob' argument states the probability the augment is applied, and `applyIndices' gives indices of
    the arrays to apply to (or None for all). The arguments are also keyword arguments in the resulting augment function.
    '''
    def _inner(func):
        @wraps(func)
        def _func(*args,**kwargs):
            _prob=kwargs.pop('prob',prob)
            
            if _prob<1.0 and not trainutils.randChoice(_prob):
                return args
            
            _applyIndices=kwargs.pop('applyIndices',applyIndices)
            
            op=func(*args,**kwargs)
            indices=list(_applyIndices or range(len(args)))
            
            return tuple((op(im) if i in indices else im) for i,im in enumerate(args))
        
        if _func.__doc__:
            _func.__doc__+='''
            
Keyword arg "prob": probability of applying this augment (default: 0.5)
Keyword arg "applyIndices": indices of arrays to apply augment to (default: None meaning all)
'''        
        return _func
    
    return _inner
            

@augment()
def transpose(*arrs):
    '''Transpose axes 0 and 1 for each of `arrs'.'''
    return partial(np.swapaxes,axis1=0,axis2=1)


@augment()
def flip(*arrs):
    '''Flip each of `arrs' with a random choice of up-down or left-right.'''
    return np.fliplr if trainutils.randChoice() else np.flipud


@augment()
def rot90(*arrs):
    '''Rotate each of `arrs' a random choice of quarter, half, or three-quarter circle rotations.'''
    return partial(np.rot90,k=np.random.randint(1,3))
        

@augment(prob=1.0)
def normalize(*arrs):
    '''Normalize each of `arrs'.'''
    return trainutils.rescaleArray


@augment(prob=1.0)
def randPatch(*arrs,patchSize=(32,32),maxcount=10, nonzeroIndex=-1):
    '''
    Randomly choose a patch from `arrs' of dimensions `patchSize'. if `nonzeroIndex' is not -1, the patch will be chosen 
    so that the image at index `nonzeroIndex' has positive non-zero pixels in it, this can be used to ensure the chosen 
    patch includes segmented features not in the background. 
    '''
    testim=arrs[nonzeroIndex]
    h,w=testim.shape[:2]
    ph,pw=patchSize
    ry=np.random.randint(0,h-ph)
    rx=np.random.randint(0,w-pw)
    
    if nonzeroIndex!=-1:
        for i in range(maxcount):
            if testim[ry:ry+ph,rx:rx+pw].max()>0:
                break
            
            ry=np.random.randint(0,h-ph)
            rx=np.random.randint(0,w-pw)

    return lambda im: im[ry:ry+ph,rx:rx+pw]


@augment()
def shift(*arrs,margin=5,dimfract=2,order=3,maxcount=10, nonzeroIndex=-1):
    '''Shift arrays randomly by `dimfract' fractions of the array dimensions.'''
    testim=arrs[nonzeroIndex]
    x,y=testim.shape[:2]
    seg=testim.astype(np.int)
    shiftx=np.random.randint(-x//dimfract,x//dimfract)
    shifty=np.random.randint(-y//dimfract,y//dimfract)
    
    def _shift(im):
        sval=(shiftx,shifty)+tuple(0 for _ in range(2,im.ndim))
        return scipy.ndimage.shift(im,sval,order=order)
    
    if nonzeroIndex!=-1:
        for i in range(maxcount):
            seg=_shift(testim).astype(np.int32)
            if trainutils.zeroMargins(seg,margin):
                break
            
            shiftx=np.random.randint(-x//dimfract,x//dimfract)
            shifty=np.random.randint(-y//dimfract,y//dimfract)
            
    return _shift


@augment()
def rotate(*arrs,margin=5,maxcount=10,nonzeroIndex=-1):
    '''Shift arrays randomly around the array center.'''
    
    angle=np.random.random()*360
    
    _rotate=partial(scipy.ndimage.rotate,angle=angle,reshape=False)
    
    if nonzeroIndex!=-1:
        testim=arrs[nonzeroIndex]
        
        for i in range(maxcount):
            seg=_rotate(testim).astype(np.int32)
            if trainutils.zeroMargins(seg,margin):
                break
            
            angle=np.random.random()*360
        
    return _rotate


@augment()
def zoom(*arrs,margin=5,zoomrange=0.2,maxcount=10,nonzeroIndex=-1):
    '''Return the image/mask pair zoomed by a random amount with the mask kept within `margin' pixels of the edges.'''
    
    z=zoomrange-np.random.random()*zoomrange*2
    zx=z+1.0+zoomrange*0.25-np.random.random()*zoomrange*0.5
    zy=z+1.0+zoomrange*0.25-np.random.random()*zoomrange*0.5
        
    def _zoom(im):
        ztemp=scipy.ndimage.zoom(im,(zx,zy)+tuple(1 for _ in range(2,im.ndim)),order=2)
        return trainutils.resizeCenter(ztemp,*im.shape)
    
    if nonzeroIndex!=-1:
        testim=arrs[nonzeroIndex]
        
        for i in range(maxcount):
            seg=_zoom(testim).astype(np.int32)
            if trainutils.zeroMargins(seg,margin):
                break
            
            z=zoomrange-np.random.random()*zoomrange*2
            zx=z+1.0+zoomrange*0.25-np.random.random()*zoomrange*0.5
            zy=z+1.0+zoomrange*0.25-np.random.random()*zoomrange*0.5
            
    return _zoom

  
@augment()
def deformPIL(*arrs,defrange=25,numControls=3,margin=2):
    '''Deforms arrays randomly with a deformation grid of size `numControls'**2 with `margins' grid values fixed.'''
    from PIL import Image
    
    h,w = arrs[0].shape[:2]
    
    imshift=np.zeros((2,numControls+margin*2,numControls+margin*2))
    imshift[:,margin:-margin,margin:-margin]=np.random.randint(-defrange,defrange,(2,numControls,numControls))

    imshiftx=np.array(Image.fromarray(imshift[0]).resize((w,h),Image.QUAD))
    imshifty=np.array(Image.fromarray(imshift[1]).resize((w,h),Image.QUAD))
        
    y,x=np.meshgrid(np.arange(w), np.arange(h))
    indices=np.reshape(x+imshiftx, (-1, 1)),np.reshape(y+imshifty, (-1, 1))

    def _mapChannels(im):
        if im.ndim==2:
            result=scipy.ndimage.map_coordinates(im,indices, order=1, mode='constant')
        else:
            result=np.concatenate([_mapChannels(im[...,i]) for i in range(im.shape[-1])])
            
        return result.reshape(im.shape)
    
    return _mapChannels


@augment()
def distortFFT(*arrs,minDist=0.1,maxDist=1.0):
    '''Distorts arrays by applying dropout in k-space with a per-pixel probability based on distance from center.'''
    h,w=arrs[0].shape[:2]

    x,y=np.meshgrid(np.linspace(-1,1,h),np.linspace(-1,1,w))
    probfield=np.sqrt(x**2+y**2)
    
    if arrs[0].ndim==3:
        probfield=np.repeat(probfield[...,np.newaxis],arrs[0].shape[2],2)
    
    dropout=np.random.uniform(minDist,maxDist,arrs[0].shape)>probfield

    def _distort(im):
        im=ft.fft2(im)
        im=ft.fftshift(im)
        im=im*dropout
        im=ft.ifft2(im)
        im=np.abs(im)
        return im
    
    return _distort


if __name__=='__main__':
    
    im=np.random.rand(128,128,1)
    
    imt=transpose(im,prob=1.0)
    print(np.all(im.T==imt[0]))
    
    imf=flip(im,prob=1.0)
    imr=rot90(im,prob=1.0)
    
    print(randPatch(im,patchSize=(30,34))[0].shape)
    
    print(shift(im,prob=1.0)[0].shape)
    
    print(rotate(im,prob=1.0)[0].shape)
    
    print(zoom(im,prob=1.0)[0].shape)
    
    print(deformPIL(im,prob=1.0)[0].shape)
    
    print(distortFFT(im,prob=1.0)[0].shape)
    