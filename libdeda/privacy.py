# -*- coding: utf-8 -*- 
"""
Privacy and Anonymisation functions

Copyright 2018 Timo Richter

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.
"""

import sys, json, cv2
import numpy as np
from scipy.stats import circmean
from io import BytesIO
from itertools import combinations
try: import Image
except ImportError: from PIL import Image
try: import wand
except ImportError: pass
else: from wand.image import Image as WandImage
import libdeda.pypdf2patch
from PyPDF2 import PdfFileReader, PdfFileWriter
from reportlab.pdfgen import canvas
from reportlab.lib import pagesizes
from colorsys import rgb_to_hsv
from libdeda.print_parser import PrintParser, patternDict, prototypeHps, \
    prototypeVps
from libdeda.extract_yd import rotateImage, matrix2str, ImgProcessingMixin
from libdeda.cmyk_to_rgb import CYAN, MAGENTA, BLACK, YELLOW
from libdeda.pattern_handler import _AbstractMatrixParser


MASK_VERSION = 3

CALIBRATIONPAGE_SIZE = pagesizes.A4
EDGE_COLOUR = MAGENTA
EDGE_MARGIN = 2.0/6 # inches
MARKER_SIZE = 5.0/72
    
CALIBRATION_MARKERS = [(x,y)
    for x in [0+EDGE_MARGIN, CALIBRATIONPAGE_SIZE[0]/72-EDGE_MARGIN]
    for y in [0+EDGE_MARGIN, CALIBRATIONPAGE_SIZE[1]/72-EDGE_MARGIN]]

    
def createCalibrationpage():
    """
    Create the calibration page according to the global parameters and return
    the PDF binary
    """
    
    imdpi=600
    io = BytesIO()
    c = canvas.Canvas(io, pagesize=CALIBRATIONPAGE_SIZE)
    im = np.zeros((int(CALIBRATIONPAGE_SIZE[1]),
        int(CALIBRATIONPAGE_SIZE[0]),3))
    im[:] = 255
    
    c.setFillColorRGB(*EDGE_COLOUR)
    for x,y in CALIBRATION_MARKERS:
        c.rect(x*72,y*72,MARKER_SIZE*72,MARKER_SIZE*72, stroke=0, fill=1)
        im[int((y-MARKER_SIZE)*imdpi)+1:int(y*imdpi)+1,
            int(x*imdpi):int((x+MARKER_SIZE)*imdpi)] = tuple(reversed(EDGE_COLOUR))
    
    c.setFillColorRGB(*CYAN)
    c.rect(
        (EDGE_MARGIN+MARKER_SIZE)*72,EDGE_MARGIN*72,
        MARKER_SIZE*72,MARKER_SIZE*72,
        stroke=0, fill=1)
    im[int(-EDGE_MARGIN*imdpi-MARKER_SIZE*imdpi)+1:int(-EDGE_MARGIN*imdpi)+1,
        int(EDGE_MARGIN*imdpi+MARKER_SIZE*imdpi):int(EDGE_MARGIN*imdpi+2*MARKER_SIZE*imdpi)] = tuple(reversed(CYAN))
    
    c.showPage()
    c.save()
    #cv2.imwrite("testpage.png",im)
    io.seek(0)
    pdfbin = io.read()
    return pdfbin


class AnonmaskCreator(object):
    """
    Parse scanned calibration page and create prototype of anonymisation mask
    that can be applied on pages of any size by AnonmaskApplier
    """

    def __init__(self,imbin):
        """
        @imbin: Image binary of scanned calibration page image
        """
        file_bytes = np.asarray(bytearray(imbin), dtype=np.uint8)
        self.im = cv2.imdecode(file_bytes, cv2.IMREAD_COLOR)
        try:
                dpi = [float(n) for n in Image.open(BytesIO(imbin)).info["dpi"]]
                self.dpi = np.average(dpi)
                if self.dpi == 72: raise ValueError("assuming wrong dpi")
        except (KeyError, ValueError):
            self.dpi = round(max(self.im.shape)/(max(CALIBRATIONPAGE_SIZE)/72),-2)
            sys.stderr.write("Warning: Cannot determine dpi of input. "
                +"Assuming %f dpi.\n"%self.dpi)

    def __call__(self, *args, **xargs):
        self.restoreOrientation()
        self.restoreSkewByDots()
        #self.restoreSkewByMarkers()
        self._magentaMarkers = self._getMagentaMarkers()
        self.getPageScaling()
        self.restorePerspective()
        maskdata = self.createMask(*args, **xargs)
        return json.dumps(maskdata).encode("ascii")
        
    @property
    def centre(self):
        return (self.im.shape[1]/2, self.im.shape[0]/2)
            
    def _selectColour(self,colour):
        edgeHue = int(rgb_to_hsv(*colour)[0]*180)
        TOLERANCE = 30
        edgeHue1 = (edgeHue-TOLERANCE)%181
        edgeHue2 = (edgeHue+TOLERANCE)%181
        hsv = cv2.cvtColor(self.im, cv2.COLOR_BGR2HSV)
        if edgeHue2 < edgeHue1: 
            raise Exception("Implementation change necessary: "
                +"Trying to select range that includes 180..0.")
        im = cv2.inRange(hsv, (edgeHue1,100,100),(edgeHue2,255,255))
        return im

    def _findContours(self, colour):
        im = self._selectColour(colour)
        contours = np.array(cv2.findContours(
            im,cv2.RETR_EXTERNAL,cv2.CHAIN_APPROX_SIMPLE)[-2],
            dtype=np.object)
        area = np.array([(np.max(contour[:,0])-np.min(contour[:,0]))
                *(np.max(contour[:,1])-np.min(contour[:,1]))
            for e in contours for contour in [e[:,0]]])/self.dpi**2
        return contours[(area > MARKER_SIZE**2/2)*(area < MARKER_SIZE**2*2)]
    
    def restoreOrientation(self):
        contours = self._findContours(CYAN)
        cEdges = np.array([c[0] for e in contours for c in e])
        dot = np.min(cEdges[:,0]), np.min(cEdges[:,1])
        angles = {False: {False: 3, True: 0}, True: {False: 2, True: 1}}
        angle = angles[dot[0]>self.centre[0]][dot[1]>self.centre[1]]
        print("Orientation correction: rotating input by %d°"%(angle*90))
        self.im = np.rot90(self.im, angle)
    
    def _getMagentaMarkers(self):
        contours = self._findContours(EDGE_COLOUR)
        if len(contours) == 0: 
                raise Exception("No contour found for colour EDGE_COLOUR")
        cEdges = np.array([c[0] for e in contours for c in e])
        #"""
        def getEdge(comp1, comp2):
            # get all contours in the quarter of the sheet selected by
            # comp1 and comp2
            area = cEdges[getattr(cEdges[:,0],comp1)(self.centre[0])
                *getattr(cEdges[:,1],comp2)(self.centre[1])]
            return np.min(area[:,0]), np.max(area[:,1])
        comparators = ['__le__','__ge__']
        # list of coords with the bottom left edges of the four magenta
        # squares. order: topleft, bottomleft, topright, bottomright
        edges = [getEdge(c1,c2) for c1 in comparators for c2 in comparators]
        inputPoints = np.array(edges,dtype=np.float32)
        return inputPoints
        
    def restoreSkewByMarkers(self):
        _,_, angle = cv2.minAreaRect(self._getMagentaMarkers())
        angle = angle%90 if angle%90<45 else angle%90-90
        print("Skew correction: rotating by %+f°"%angle)
        self.im = rotateImage(self.im, angle, cv2.INTER_NEAREST)
    
    def restoreSkewByDots(self):
        pp = PrintParser(self.im,ydxArgs=dict(
            inputDpi=self.dpi,interpolation=cv2.INTER_NEAREST))
        self.im = rotateImage(self.im, -pp.yd.rotation)
        print("Rotating by %f°"%-pp.yd.rotation)
        
    def getPageScaling(self):
        dist = lambda p1,p2:(abs(p1[0]-p2[0])**2+abs(p1[1]-p2[1])**2)**.5
        scannedMarkers = [(x/self.dpi,y/self.dpi) 
            for (x,y) in self._magentaMarkers.tolist()]
        distsScan = [dist(a,b) for a,b in combinations(scannedMarkers,2)]
        distsReal = [dist(a,b) for a,b in combinations(CALIBRATION_MARKERS,2)
            ]
        dists = [a/b for a,b in zip(distsReal,distsScan)]
        self.scaling = np.max(dists)
        if abs(1-self.scaling) > 0.01: sys.stderr.write(
            "WARNING: Your print has probably been resized. For better "    
            "results disable \"fit to page\".\n")
        #self.scaling = 1.041
        print("Scaling factor: %f theoretical inches = 1 inch on paper"
            %self.scaling)
            
    def restorePerspective(self):
        inputPoints = self._magentaMarkers
        outputPoints = np.array([(x*self.dpi, y*self.dpi) 
            for x,y in CALIBRATION_MARKERS],dtype=np.float32)
        l = cv2.getPerspectiveTransform(inputPoints,outputPoints)
        print("Perspective Transform")
        for (x1,y1),(x2,y2) in zip(inputPoints,outputPoints):
            print("\tMapping %d,%d -> %d,%d"%(x1,y1,x2,y2))
        testpageSizePx = (
            int(CALIBRATIONPAGE_SIZE[0]/72*self.dpi), int(CALIBRATIONPAGE_SIZE[1]/72*self.dpi))
        self.im = cv2.warpPerspective(self.im,l,testpageSizePx,
            #flags=cv2.INTER_LINEAR+cv2.WARP_FILL_OUTLIERS)
            flags=cv2.INTER_NEAREST+cv2.WARP_FILL_OUTLIERS)
        self.dpi *= self.scaling
        
    def createMask(self, copy):
        """
        Create a mask prototype
        """
        print("Extracting tracking dots... ")
        #cv2.imwrite("perspective.png",self.im)
        # get tracking dot matrices
        pp = PrintParser(self.im,ydxArgs=dict(
            inputDpi=self.dpi,interpolation=cv2.INTER_NEAREST,
            rotation=False))
        tdms = list(pp.getAllValidTdms())
        print("\t%d valid matrices"%len(tdms))
        
        # select tdm
        #"""
        # tdms := [closest TDM at edge \forall edge \in Edges]
        # |tdms| = |Edges|
        #pp.tdm = random.choice(tdms)
        width = pp.yd.im.shape[1]*pp.yd.imgDpi
        height = pp.yd.im.shape[0]*pp.yd.imgDpi
        edges = [(0,0),(width,0),(0,height),(width,height)]
        tdms = [
            tdms[np.argmin(
                [abs(e1-tdm.atX)+abs(e2-tdm.atY) for tdm in tdms]
            )] for e1, e2 in edges]
        #"""
        
        print("\tTracking dots pattern found:")
        print("\tx=%d, y=%d, trans=%s"%(pp.tdm.atX,pp.tdm.atY,pp.tdm.trans))
        ni,nj,di,dj = patternDict[pp.pattern]
        #di = self.scaling*di
        #dj = self.scaling*dj
        hps = ni*di
        vps = nj*dj
        #tdm = pp.tdm
        #tdms = sorted(tdms,key=lambda e:e.atY+e.atX)
        """
        dots_references = [(x*di+tdm.atX/pp.yd.imgDpi, y*dj+tdm.atY/pp.yd.imgDpi)
            for tdm in tdms
            for m in [tdm.undoTransformation()]
            for x in range(m.shape[0]) for y in range(m.shape[1])
            if m[x,y] == 1]
        im = np.zeros(self.im.shape)
        for x,y in dots_references:
            im[int(y*self.dpi),int(x*self.dpi)] = 255
        cv2.imwrite("perspective_layer.png", im)
        """
        
        # create mask
        tdm = pp.tdm if copy else pp.tdm.createMask()
        #m = tdm.aligned
        #m = tdm.undoTransformation()
        #m = np.roll(m,(pp.tdm.trans["x"],pp.tdm.trans["y"]),(0,1))
        m = _AbstractMatrixParser.undoTransformation(tdm.aligned,dict(
            x=0,y=0,rot=tdm.trans["rot"],flip=tdm.trans["flip"]
        ))
        dots_proto = [((x*di), (y*dj))
            for x in range(m.shape[0]) for y in range(m.shape[1]) 
            if m[x,y] == 1]
        
        # set correct hps,vps for offset patterns
        hps2 = hps*prototypeHps[pp.pattern]
        vps2 = vps*prototypeVps[pp.pattern]
        
        # Calc xOffset and yOffset: Distance from (0,0) to the first marking
        # dots
        #xOffset = (pp.tdm.atX/pp.yd.imgDpi-pp.tdm.trans["x"]*di)%hps2
        #yOffset = (pp.tdm.atY/pp.yd.imgDpi-pp.tdm.trans["y"]*dj)%vps2
        
        xOffsets = [(tdm.atX/pp.yd.imgDpi-tdm.trans["x"]*di)%hps2
            for tdm in tdms]
        xOffset = circmean(xOffsets, high=hps2)
        yOffsets = [(tdm.atY/pp.yd.imgDpi-tdm.trans["y"]*dj)%vps2
            for tdm in tdms]
        yOffset = circmean(yOffsets, high=vps2)
        
        #self.im = rotateImage(self.im, -pp.yd.rotation)
        #print("Rotating by %f°"%-pp.yd.rotation)
        
        # find scan margin dx, dy and subtract from offset
        """
        dd = [((markerScanx/pp.yd.imgDpi-markerx/self.scaling), 
                (markerScany/pp.yd.imgDpi-markery/self.scaling))
            for (markerScanx,markerScany),(markerx,markery) 
            in zip(self._getMagentaMarkers(),CALIBRATION_MARKERS)]
        dx = np.average([x for x,y in dd])
        dy = np.average([y for x,y in dd])
        xOffset = (xOffset-dx*self.scaling)%hps2
        yOffset = (yOffset-dy*self.scaling)%vps2
        #"""
        
        if pp.tdm.trans["rot"]%2 == 1:
            xOffset, yOffset = yOffset, xOffset
        print("TDM offset: %f, %f"%(xOffset,yOffset))

        return dict(proto=dots_proto, hps=hps, vps=vps, x_offset=xOffset,
            y_offset=yOffset, pagesize=CALIBRATIONPAGE_SIZE, 
            scale=self.scaling, format_ver=MASK_VERSION)
        

def calibrationScan2Anonmask(imbin, copy=False):
    return AnonmaskCreator(imbin)(copy)
    

class AnonmaskApplier(object):
    """
    Apply the anonymisation mask created by AnonmaskCreator to a page for 
    printing
    """
    colour = YELLOW
    dotRadius = 0.004 #in

    def __init__(self, mask, dotRadius=None, xoffset=None, 
                yoffset=None, debug=False, scale=None):
        d = json.loads(mask)
        if d.get("format_ver",0) < MASK_VERSION:
            raise Exception("Incompatible format version by mask. "
                "Please generate AND print a new calibration page (see "
                "deda_anonmask_create).")
        proto = d["proto"]
        self.scale = scale or d.get("scale",1)
        self.hps = d["hps"]
        self.vps = d["vps"]
        self.xOffset = xoffset or d["x_offset"]
        self.yOffset = yoffset or d["y_offset"]
        
        self.proto = [
            ((xDot+self.xOffset)%self.hps, (yDot+self.yOffset)%self.vps)
            for xDot, yDot in proto]
        self.pagesize = d["pagesize"]
        if dotRadius: self.dotRadius = dotRadius
        if debug: self.colour = MAGENTA

    def apply(self, inPdf):
        inPdf = self.pdfNormaliseFormat(inPdf,*self.pagesize) # force A4
        if "wand" in globals():
            return self.pdfWatermark(inPdf, self._createMask, True)
        else: 
            self.maskpdf = self._createMask()
            return self.pdfWatermark(inPdf, lambda *args:self.maskpdf, False)
  
    def _createMask(self,page=None):
        w,h = self.pagesize
        w = float(w)
        h = float(h)
        shps = self.hps*self.scale
        svps = self.vps*self.scale
        io = BytesIO()
        c = canvas.Canvas(io, pagesize=(w,h) )
        c.setStrokeColorRGB(*self.colour)
        c.setFillColorRGB(*self.colour)
        allDots = []
        for x_ in range(int(w/shps/72+1)):
            for y_ in range(int(h/svps/72+1)):
                for xDot, yDot in self.proto:
                    x = (x_*shps+xDot*self.scale)
                    y = (y_*svps+yDot*self.scale)
                    if x*72 > w or y*72 > h: continue
                    allDots.append((x,y))

        if "wand" in globals():
            # PyPDF2 page to cv2 image
            dpi = 300
            pageio = BytesIO()
            pageWriter = PdfFileWriter()
            pageWriter.addPage(page)
            pageWriter.write(pageio)
            pageio.seek(0)
            with WandImage(file=pageio,format="pdf",resolution=dpi) as wim:
                imbin = wim.make_blob("png")
            file_bytes = np.asarray(bytearray(imbin), dtype=np.uint8)
            im = cv2.imdecode(file_bytes, cv2.IMREAD_COLOR)
            
            # remove dots on black spots
            allDots = [(x,y) for x,y in allDots 
                if int(y*dpi)>=im.shape[0] or int(x*dpi)>=im.shape[1] 
                or (im[int(y*dpi),int(x*dpi)] != (0,0,0)).any()]
            
        for x,y in allDots:
            c.circle(x*72,h-y*72,self.dotRadius*72,stroke=0,fill=1)
        c.showPage()
        c.save()
        return io
    
    @staticmethod
    def pdfNormaliseFormat(pdfin, width, height):
        """
        Reads a PDF as binary string and sets with and height of each page.
        """
        input_ = PdfFileReader(BytesIO(pdfin))
        output = PdfFileWriter()
        outIO = BytesIO()
        for p_nr in range(input_.getNumPages()):
            page = input_.getPage(p_nr)
            outPage = output.addBlankPage(width, height)
            outPage.mergePage(page)
            outPage.compressContentStreams()
        output.write(outIO)
        outIO.seek(0)
        return outIO.read()
    
    @staticmethod
    def pdfWatermark(pdfin, maskCreator, foreground=False):
        """
        For each page from pdfin, maskCreator(page) will be called
        and shall return a single page watermark PDF.
        @pdfin PDF binary or file path
        @maskCreator Function that creates the watermark for each page given 
            arguments width and height,
        @foreground If False, put the watermark in the background,
        Returns: Pdf binary
        
        Example:
            def func(w,h): ...
            with open("output.pdf","wb") as fp_out:
                with open("input.pdf","rb") as fp_in:
                    fp_out.write(pdfWatermark(pf_in.read(), func))
        """
        output = PdfFileWriter()
        if not isinstance(pdfin, bytes):
            with open(pdfin,"rb") as fp: pdfin = fp.read()
        input_ = PdfFileReader(BytesIO(pdfin))
        
        for p_nr in range(input_.getNumPages()):
            page = input_.getPage(p_nr)
            mask = PdfFileReader(maskCreator(page)).getPage(0)
            if foreground:
                page.mergePage(mask)
                output.addPage(page)
            else:
                maskCp = output.addBlankPage(page.mediaBox.getWidth(),
                    page.mediaBox.getHeight())
                maskCp.mergePage(mask)
                maskCp.mergePage(page)
            output.getPage(p_nr).compressContentStreams()
        
        outIO = BytesIO()
        output.write(outIO)
        outIO.seek(0)
        return outIO.read()


class ScanCleaner(ImgProcessingMixin):
    """ Remove Yellow Dots From White Areas of Scanned Pages """
    
    _verbosity = 0
    
    def __init__(self, imbin, *args, **xargs):
        file_bytes = np.asarray(bytearray(imbin), dtype=np.uint8)
        self.im = cv2.imdecode(file_bytes, cv2.IMREAD_COLOR)
        super(ScanCleaner, self).__init__(BytesIO(imbin), *args, **xargs)
    
    def __call__(self, grayscale=False, outformat=".png"):
        _, mask = self.processImage(self.im,workAtDpi=None,halftonesBlur=10,
            ydColourRange=((16,1,214),(42,120,255)),
            paperColourThreshold=225)#233
        self.im[mask==255] = 255
        if grayscale: self.im = cv2.cvtColor(self.im, cv2.COLOR_BGR2GRAY)
        #cv2.imwrite(output,self.im)
        return cv2.imencode(outformat, self.im)[1]


def cleanScan(imbin,*args,**xargs):
    return ScanCleaner(imbin)(*args,**xargs)
    
    
