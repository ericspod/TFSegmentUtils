
from __future__ import division, print_function
import threading
import multiprocessing as mp
import platform
import queue
from multiprocessing import sharedctypes
from contextlib import contextmanager, ExitStack

import numpy as np


def toShared(array):
    '''Convert the given Numpy array to a shared ctypes object.'''
    carr=np.ctypeslib.as_ctypes(array)
    return sharedctypes.RawArray(carr._type_, carr)


def fromShared(array):
    '''Map the given ctypes object to a Numpy array, this is expected to be a shared object from the parent.'''
    return np.ctypeslib.as_array(array)
        
        
def initProc(inArrays_,augs_,augments_):
    '''Initialize subprocesses by setting global variables.'''
    global inArrays
    global augs
    global augments
    inArrays=tuple(map(fromShared,inArrays_))
    augs=tuple(map(fromShared,augs_))
    augments=augments_
    
    
def applyAugmentsProc(indices):
    '''Apply the augmentations to the input array at the given indices.'''
    global inArrays
    global augs
    global augments
    
    for i in indices:
        inarrs=[a[i] for a in inArrays]
        
        for aug in augments:
            inarrs=aug(*inarrs)
            
        for ina,outa in zip(inarrs,augs):
            outa[i]=ina
        

class DataSource(object):
    def __init__(self,*arrays,dataGen=None,selectProbs=None,augments=[]):
        self.arrays=list(arrays)
        self.dataGen=dataGen or self.defaultDataGen
        self.selectProbs=selectProbs
        self.augments=augments
        
    def defaultDataGen(self,batchSize=None,selectProbs=None,chosenInds=None):
        if chosenInds is None:
            chosenInds=np.random.choice(self.arrays[0].shape[0],batchSize,p=selectProbs)
                
        return tuple(a[chosenInds] for a in self.arrays)
    
    def stop(self,genType):
        assert genType in ('local','thread','process')
                
    def getRandomBatch(self,batchSize):
        '''Call the generator callable with the given `batchSize' value with self.selectProb as the second argument.'''
        return self.dataGen(batchSize,self.selectProbs)
    
    def getIndexBatch(self,chosenInds):
        '''Call the generator callable with `chosenInds' as the chosen indices to select values from.'''
        return self.dataGen(chosenInds=chosenInds)
    
    def getAugmentedArrays(self,arrays):
        '''Apply the augmentations to single-instance arrays.'''
        for aug in self.augments:
            arrays=aug(*arrays)
            
        return arrays
    
    def applyAugments(self,arrays,augArrays,indices=None):
        '''Apply the augmentations to batch input and output arrays at `indices' or for the whole arrays if not given.'''
        indices=range(arrays[0].shape[0]) if indices is None else indices
            
        for i in indices:
            inarrs=[a[i] for a in arrays]
            outarrs=self.getAugmentedArrays(inarrs)
            for out,aug in zip(outarrs,augArrays):
                aug[i]=out
                
    @contextmanager
    def localBatchGen(self,batchSize):
        '''Yields a callable object which produces `batchSize' batches generated in the calling thread.'''
        inArrays=self.getIndexBatch([0])
        augTest=self.getAugmentedArrays([a[0] for a in inArrays])
        
        augs=tuple(np.zeros((batchSize,)+a.shape,a.dtype) for a in augTest)
        
        def _getBatch():
            batch=self.getRandomBatch(batchSize)
            self.applyAugments(batch,augs,list(range(batchSize)))
            return augs
        
        try:
            yield _getBatch
        finally:
            self.stop('local')
                
    @contextmanager
    def threadBatchGen(self,batchSize,numThreads=None):
        '''Yields a callable object which produces `batchSize' batches generated in `numThreads' threads.'''
        numThreads=min(batchSize,numThreads or mp.cpu_count())
        threadIndices=np.array_split(np.arange(batchSize),numThreads)
        isRunning=True
        batchQueue=queue.Queue(1)
        
        with self.localBatchGen(batchSize) as gen:
            augs=gen()
        
        def _batchThread():
            while isRunning:
                threads=[]
                batch=self.getRandomBatch(batchSize)
                
                for indices in threadIndices:
                    t=threading.Thread(target=self.applyAugments,args=(batch,augs,indices))
                    t.start()
                    threads.append(t)
                    
                for t in threads:
                    t.join()
                    
                batchQueue.put(tuple(a.copy() for a in augs)) # copy to prevent overwriting arrays before they're used
                
        batchThread=threading.Thread(target=_batchThread)
        batchThread.start()
        
        try:
            yield batchQueue.get
        finally:
            isRunning=False
            self.stop('thread')
            try:
                batchQueue.get(True) # there may be a batch waiting on the queue, batchThread is stuck until this is removed
            except queue.Empty:
                pass
            
    @contextmanager
    def processBatchGen(self,batchSize,numProcs=None):
        '''Yields a callable object which produces `batchSize' batches generated in `numProcs' subprocesses.'''
        assert platform.system().lower()!='windows', 'Generating batches with processes requires fork() semantics not present in Windows.'
        
        numProcs=min(batchSize,numProcs or mp.cpu_count())
        procIndices=np.array_split(np.arange(batchSize),numProcs)
        isRunning=True
        batchQueue=mp.Queue(1)
        
        with self.localBatchGen(batchSize) as gen:
            augs=tuple(map(toShared,gen()))
        
        inArrays=tuple(map(toShared,self.getIndexBatch(np.arange(batchSize))))
        
        maugs=self.augments
        initargs=(inArrays,augs,maugs)
               
        def _batchThread(inArrays,augs,maugs):
            try:
                initargs=(inArrays,augs,maugs)
                
                with mp.Pool(numProcs,initializer=initProc,initargs=initargs) as p:
                    inArrays=tuple(map(fromShared,inArrays))
                    augs=tuple(map(fromShared,augs))
                        
                    while isRunning:
                        batch=self.getRandomBatch(batchSize)
                        for a,b in zip(inArrays,batch):
                            a[...]=b
                            
                        if maugs:
                            p.map(applyAugmentsProc,procIndices)

                        batchQueue.put(tuple(a.copy() for a in augs))
                        
            except Exception as e:
                batchQueue.put(e)
                
        batchThread=threading.Thread(target=_batchThread,args=initargs)
        batchThread.start()
        
        def _get():
            v=batchQueue.get()
            if not isinstance(v,tuple):
                raise v
                
            return v
        
        try:
            yield _get
        finally:
            isRunning=False
            self.stop('process')
            try:
                batchQueue.get(True) # there may be a batch waiting on the queue, batchThread is stuck until this is removed
            except queue.Empty:
                pass
        
        
def randomDataSource(shape,augments=[],dtype=np.float32):
    '''
    Returns a DataSource producing batches of `shape'-sized standard normal random arrays of type `dtype'. The `augments'
    list of augmentations is pass to the DataSource object's constructor. Each batch contains the same array given twice.
    '''
    def randData(batchSize=None,selectProbs=None,chosenInds=None):
        if chosenInds: # there are no arrays to index from so use the list size as batchSize instead
            batchSize=len(chosenInds)

        randvals=np.random.randn(batchSize, *shape).astype(dtype)
        return randvals,randvals
    
    return DataSource(dataGen=randData,augments=augments)

        
class BufferDataSource(DataSource):
    def appendBuffer(self,*arrays):
        if not self.arrays:
            self.arrays=list(arrays)
        else:
            for i in range(len(self.arrays)):
                self.arrays[i]=np.concatenate([self.arrays[i],arrays[i]])
                
        if self.selectProbs is not None:
            size=self.arrays[0].shape[0]
            self.selectProbs=np.ones((size,))/size
            
    def clearBuffer(self):
        if self.bufferSize()>0:
            self.arrays=[]
            
            if self.selectProbs is not None:
                self.selectProbs=self.selectProbs[:0]
            
    def bufferSize(self):
        return 0 if not self.arrays else self.arrays[0].shape[0]

    
class MergeDataSource(DataSource):
    def __init__(self,*srcs,numThreads=None,augments=[]):
        self.srcs=srcs
        self.batchSize=0
        self.gen=None
        self.numThreads=numThreads
        
        DataSource.__init__(self,dataGen=self._dataGen,augments=augments)
    
    def stop(self,getType):
        self.gen=None
            
    def _setBatchSize(self,batchSize):
        def yieldData():
            with ExitStack() as stack:
                gens=[stack.enter_context(s.threadBatchGen(self.batchSize,self.numThreads)) for s in self.srcs]
                while self.gen is not None:
                    yield sum([g() for g in gens],())
                    
        if self.gen is None or self.batchSize!=batchSize:
            self.batchSize=batchSize
            self.gen=yieldData()
        
    def _dataGen(self,batchSize=None,selectProbs=None,chosenInds=None):
        if batchSize is None:
            batchSize=len(chosenInds)
            
        self._setBatchSize(batchSize)
        return next(self.gen) 
        

class FileDataSource(DataSource):
    def __init__(self,*filelists,maxSize=100*(2**20), selectProbs=None,augments=[]):
        assert all(len(f)==len(filelists[0]) for f in filelists), "All members of `filelists' must be the same length"
        
        import imageio
        self.iio=imageio
        import PIL
        self.image=PIL.Image
        
        self.imageCache={}
        self.currentSize=0
        self.maxSize=maxSize
        super().__init__(*list(map(np.asarray,filelists)),dataGen=self._dataGen,selectProbs=selectProbs,augments=augments)
        
    def loadFile(self,path):
#        return self.iio.imread(path)
        return np.asarray(self.image.open(path)).copy()
        
    def _getCachedFile(self,path):
        if path not in self.imageCache:
            im=self.loadFile(path)
            self.currentSize+=im.nbytes
            self.imageCache[path]=im
            
        return self.imageCache[path]

    def _removeFiles(self):
        if self.maxSize<=0:
            return 
        
        candidates=list(self.imageCache)        
        while len(candidates)>0 and self.currentSize>self.maxSize:
            c=candidates.pop(0)
            im=self.imageCache[c]
            self.currentSize-=im.nbytes
            del self.imageCache[c]
        
    def _dataGen(self,batchSize=None,selectProbs=None,chosenInds=None):
        if chosenInds is None:
            chosenInds=np.random.choice(self.arrays[0].shape[0],batchSize,p=selectProbs)
            
        outs=[]
        for arr in self.arrays:
            chosen=arr[chosenInds]
            im=self._getCachedFile(chosen[0])
            out=np.zeros((len(chosenInds),)+im.shape,im.dtype)
            
            for i,c in enumerate(chosen):
                out[i]=self._getCachedFile(c)
                
            outs.append(out)
            
        self._removeFiles()
        
        return tuple(outs)
    
    
if __name__=='__main__':
    def testAug(im,cat):
        return im[0],cat

    src=DataSource(np.random.randn(10,1,16,16),np.random.randn(10,2),augments=[testAug])
    
    with src.processBatchGen(4) as gen:
        batch=gen()
        
        print([a.shape for a in batch])
        
    bsrc=BufferDataSource()#np.random.randn(0,1,16,16),np.random.randn(0,2))
    
    bsrc.appendBuffer(np.random.randn(10,1,16,16),np.random.randn(10,2))
    
    with bsrc.processBatchGen(4) as gen:
        batch=gen()
        print([a.shape for a in batch])
    
#     bsrc.clearBuffer()
        
#     with bsrc.processBatchGen(4) as gen:
#         batch=gen()
#         print([a.shape for a in batch])

    merge=MergeDataSource([src])
    with merge.processBatchGen(4) as gen:
        batch=gen()
        print([b.shape for b in batch])
        
    print('Done')