#!/usr/bin/python
"""
  Basic class for communication to Parrot Bebop
  usage:
       ./bebop.py <task> [<metalog> [<F>]]
"""
import sys
import socket
import datetime
import struct
import time
import numpy
import subprocess as sp

from navdata import *
from commands import *
from video import VideoFrames

# this will be in new separate repository as common library fo robotika Python-powered robots
from apyros.metalog import MetaLog, disableAsserts
from apyros.manual import myKbhit, ManualControlException


# hits from https://github.com/ARDroneSDK3/ARSDKBuildUtils/issues/5

HOST = "192.168.42.1"
DISCOVERY_PORT = 44444

NAVDATA_PORT = 43210 # d2c_port
COMMAND_PORT = 54321 # c2d_port

class JpegReader(Thread):
    def __init__(self, drone, fps):
        Thread.__init__(self)
        self.fps = fps
        self.interval = 0
        if self.fps > 0:
            self.interval = 1000000 // self.fps
        self.drone = drone
        self.command = ["ffmpeg", '-i', '-', '-f', 'image2pipe', '-pix_fmt', 'bgr24', '-q:v', '1', '-vcodec', 'rawvideo', '-']
        self.ffmpeg = sp.Popen(self.command, stdin=sp.PIPE, stdout=sp.PIPE, bufsize=10 ** 8)

    def run(self):
        ms = datetime.datetime.now()
        sum = 0
        while True:
            raw_image = self.ffmpeg.stdout.read(640 * 368 * 3)

            # transform the byte read into a numpy array
            if len(raw_image) != 640 * 368 * 3:
                break
            image = numpy.fromstring(raw_image, dtype='uint8')
            image = image.reshape((368, 640, 3))

            diff = datetime.datetime.now() - ms
            sum = sum + diff.microseconds
            #print(diff.microseconds, self.interval, self.fps)

            if self.drone.videoCbk and sum > self.interval:
                self.drone.videoCbk(image, self.drone, False)

            if sum > self.interval:
                sum = 0

            ms = datetime.datetime.now()

    def appendFrame(self, data):
        self.ffmpeg.stdin.write(data)
        self.ffmpeg.stdin.flush()


class Bebop:
    def __init__( self, metalog=None, onlyIFrames=True, jpegStream=False, fps = 0 ):
        if metalog is None:
            self._discovery()
            metalog = MetaLog()
        self.navdata = metalog.createLoggedSocket( "navdata", headerFormat="<BBBI" )
        self.navdata.bind( ('',NAVDATA_PORT) )
        if metalog.replay:
            self.commandSender = CommandSenderReplay(metalog.createLoggedSocket( "cmd", headerFormat="<BBBI" ),
                    hostPortPair=(HOST, COMMAND_PORT), checkAsserts=metalog.areAssertsEnabled())
        else:
            self.commandSender = CommandSender(metalog.createLoggedSocket( "cmd", headerFormat="<BBBI" ),
                    hostPortPair=(HOST, COMMAND_PORT))
        self.console = metalog.createLoggedInput( "console", myKbhit ).get
        self.metalog = metalog
        self.buf = ""
        self.jpegStream = jpegStream
        self.fps = fps
        self.videoCbk = None
        self.videoCbkResults = None
        self.battery = None
        self.flyingState = None
        self.flatTrimCompleted = False
        self.manualControl = False
        self.time = None
        self.altitude = None
        self.position = (0,0,0)
        self.speed = (0,0,0)
        self.positionGPS = None
        self.cameraTilt, self.cameraPan = 0,0
        self.lastImageResult = None
        self.navigateHomeState = None
        self.config()
        self.commandSender.start()

        # Video variables
        self.frameWidth = 0
        self.frameHeight = 0
        self.pictureBoolean = False
        self.barcodes = []

        # Sphero tracking variables
        self.findSphero = False
        self.sinceLastSphero = 0
        self.lastFrame = None
        self.thisFrame = None
        self.moveScaler = 0
        self.objectCenterX = 0
        self.objectCenterY = 0
        self.minEdgeVal = 0
        self.maxEdgeVal = 0
        self.minCircleRadius = 0
        self.maxCircleRadius = 0

        if self.jpegStream:
            self.videoFrameProcessor = VideoFrames(onlyIFrames=False, verbose=False)
            self.reader = JpegReader(self, self.fps)
            self.reader.setDaemon(True)
            self.reader.start()
        else:
            self.videoFrameProcessor = VideoFrames(onlyIFrames=onlyIFrames, verbose=False)

    def _discovery( self ):
        "start communication with the robot"
        filename = "tmp.bin" # TODO combination outDir + date/time
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM) # TCP
        s.connect( (HOST, DISCOVERY_PORT) )
        s.send( '{"controller_type":"computer", "controller_name":"katarina", "d2c_port":"43210"}' )
        f = open( filename, "wb" )
        while True:
            data = s.recv(10240)
            if len(data) > 0:
                f.write(data)
                f.flush()
                break
        f.close()
        s.close()

    def _update( self, cmd ):
        "internal send command and return navdata"

        if not self.manualControl:
            self.manualControl = self.console()
            if self.manualControl:
                # raise exception only once
                raise ManualControlException()

        # send even None, to sync in/out queues
        self.commandSender.send( cmd )

        while len(self.buf) == 0:
            data = self.navdata.recv(40960)
            self.buf += data
        data, self.buf = cutPacket( self.buf )
        return data

    def _parseData( self, data ):
        try:
            parseData( data, robot=self, verbose=False )
        except AssertionError, e:
            print("AssertionError", e)


    def update( self, cmd=None, ackRequest=False ):
        "send command and return navdata"
        if cmd is None:
            data = self._update( None )
        else:
            data = self._update( packData(cmd, ackRequest=ackRequest) )
        while True:
            if ackRequired(data):
                self._parseData( data )
                data = self._update( createAckPacket(data) )
            elif pongRequired(data):
                self._parseData( data ) # update self.time
                data = self._update( createPongPacket(data) )
            elif videoAckRequired(data):
                if self.videoCbk:
                    self.videoFrameProcessor.append( data )
                    frame = self.videoFrameProcessor.getFrameEx()
                    if frame:
                        if not self.jpegStream:
                            self.videoCbk( frame, self, debug=self.metalog.replay )
                        else:
                            self.reader.appendFrame(frame[-1])
                    if self.videoCbkResults:
                        ret = self.videoCbkResults()
                        if ret is not None:
                            print(ret)
                            self.lastImageResult = ret
                data = self._update( createVideoAckPacket(data) )
            else:
                break
        self._parseData( data )
        return data

    def setVideoCallback( self, cbk, cbkResult=None ):
        "set cbk for collected H.264 encoded video frames & access to results queue"
        self.videoCbk = cbk
        if cbkResult is None:
            self.videoCbkResults = None
        else:
            self.videoCbkResults = self.metalog.createLoggedInput( "cv2", cbkResult ).get

    def config( self ):
        # initial cfg
        dt = self.metalog.now()
        if dt: # for compatibility with older log files
            self.update( cmd=setDateCmd( date=dt.date() ) )
            self.update( cmd=setTimeCmd( time=dt.time() ) )
        for cmd in setSpeedSettingsCmdList( maxVerticalSpeed=1.0, maxRotationSpeed=90.0,
                hullProtection=True, outdoor=True ):
            self.update( cmd=cmd )
        self.update( cmd=requestAllStatesCmd() )
        self.update( cmd=requestAllSettingsCmd() )
        self.moveCamera( tilt=self.cameraTilt, pan=self.cameraPan )
        self.update( videoAutorecordingCmd( enabled=False ) )


    def takeoff( self ):
        self.update( videoRecordingCmd( on=True ) )
        for i in xrange(10):
            #print(i,)
            self.update( cmd=None )
        print("Taking off ...",)
        self.update( cmd=takeoffCmd() )
        prevState = None
        for i in xrange(100):
            #print(i,)
            self.update( cmd=None )
            if self.flyingState != 1 and prevState == 1:
                break
            prevState = self.flyingState
        print("FLYING")

    def land( self ):
        print("Landing ...",)
        self.update( cmd=landCmd() )
        for i in xrange(100):
            #print(i,)
            self.update( cmd=None )
            if self.flyingState == 0: # landed
                break
        print("LANDED")
        self.update( videoRecordingCmd( on=False ) )
        for i in xrange(30):
            #print(i,)
            self.update( cmd=None )


    def hover( self ):
        self.update( cmd=movePCMDCmd( active=True, roll=0, pitch=0, yaw=0, gaz=0 ) )

    def emergency( self ):
        self.update( cmd=emergencyCmd() )

    def trim( self ):
        print("Trim:",)
        self.flatTrimCompleted = False
        for i in xrange(10):
            #print(i,)
            self.update( cmd=None )
        print
        self.update( cmd=trimCmd() )
        for i in xrange(10):
            #print(i,)
            self.update( cmd=None )
            if self.flatTrimCompleted:
                break

    def takePicture( self ):
        self.update( cmd=takePictureCmd() )

    def videoEnable( self ):
        "enable video stream"
        self.update( cmd=videoStreamingCmd( enable=True ), ackRequest=True )

    def videoDisable( self ):
        "enable video stream"
        self.update( cmd=videoStreamingCmd( enable=False ), ackRequest=True )

    def moveCamera( self, tilt, pan ):
        "Tilt/Pan camera consign for the drone (in degrees)"
        self.update( cmd=moveCameraCmd( tilt=tilt, pan=pan) )
        self.cameraTilt, self.cameraPan = tilt, pan # maybe move this to parse data, drone should confirm that

    def resetHome( self ):
        self.update( cmd=resetHomeCmd() )


    def wait( self, duration ):
        print("Wait", duration)
        assert self.time is not None
        startTime = self.time
        while self.time-startTime < duration:
            self.update()

    def flyToAltitude( self, altitude, timeout=3.0 ):
        print("Fly to altitude", altitude, "from", self.altitude)
        speed = 20 # 20%
        assert self.time is not None
        assert self.altitude is not None
        startTime = self.time
        if self.altitude > altitude:
            while self.altitude > altitude and self.time-startTime < timeout:
                self.update( movePCMDCmd( True, 0, 0, 0, -speed ) )
        else:
            while self.altitude < altitude and self.time-startTime < timeout:
                self.update( movePCMDCmd( True, 0, 0, 0, speed ) )
        self.update( movePCMDCmd( True, 0, 0, 0, 0 ) )

    def frontFlip(self):
        self.update( cmd=frontFlipCmd() )
        for i in xrange(10):
            self.update( movePCMDCmd( True, 0, 0, 0, 0 ) )
        self.update(cmd=None)

    def backFlip(self):
        self.update( cmd=backFlipCmd() )
        for i in xrange(10):
            self.update( movePCMDCmd( True, 0, 0, 0, 0 ) )
        self.update(cmd=None)

    def rightFlip(self):
        self.update( cmd=rightFlipCmd() )
        for i in xrange(10):
            self.update( movePCMDCmd( True, 0, 0, 0, 0 ) )
        self.update(cmd=None)

    def leftFlip(self):
        self.update( cmd=leftFlipCmd() )
        for i in xrange(10):
            self.update( movePCMDCmd( True, 0, 0, 0, 0 ) )
        self.update(cmd=None)

    def rename(self, name):
        self.update( cmd=rename(name) )
        self.wait(5)
        self.update( cmd=requestAllSettingsCmd() )


###############################################################################################

def testCamera( robot ):
    for i in xrange(10):
        print(-i,)
        robot.update( cmd=None )
    robot.resetHome()
    robot.videoEnable()
    for i in xrange(100):
        #print(i,)
        robot.update( cmd=None )
        robot.moveCamera( tilt=i, pan=i ) # up & right


def testEmergency( robot ):
    "test of reported state change"
    robot.takeoff()
    robot.emergency()
    for i in xrange(10):
        #print(i,)
        robot.update( cmd=None )


def testTakeoff( robot ):
    robot.videoEnable()
    robot.takeoff()
    for i in xrange(100):
        #print(i,)
        robot.update( cmd=None )
    robot.land()
    for i in xrange(100):
        #print(i,)
        robot.update( cmd=None )
    print


def testManualControlException( robot ):
    robot.videoEnable()
    try:
        robot.trim()
        robot.takeoff()
        robot.land()
    except ManualControlException, e:
        print
        print("ManualControlException")
        if robot.flyingState is None or robot.flyingState == 1: # taking off
            # unfortunately it is not possible to land during takeoff for ARDrone3 :(
            robot.emergency()
        robot.land()


def testFlying( robot ):
    robot.videoEnable()
    try:
        robot.trim()
        robot.takeoff()
        robot.flyToAltitude( 2.0 )
        robot.wait( 2.0 )
        robot.flyToAltitude( 1.0 )
        robot.land()
    except ManualControlException, e:
        print
        print("ManualControlException")
        if robot.flyingState is None or robot.flyingState == 1: # taking off
            # unfortunately it is not possible to land during takeoff for ARDrone3 :(
            robot.emergency()
        robot.land()


def testFlyForward( robot ):
    robot.videoEnable()
    try:
        speed = 20
        robot.trim()
        robot.takeoff()
        for i in xrange(1000):
            robot.update( cmd=movePCMDCmd( True, 0, speed, 0, 0 ) )
            print(robot.altitude)
        robot.update( cmd=movePCMDCmd( True, 0, 0, 0, 0 ) )
        robot.land()
    except ManualControlException, e:
        print
        print("ManualControlException")
        if robot.flyingState is None or robot.flyingState == 1: # taking off
            # unfortunately it is not possible to land during takeoff for ARDrone3 :(
            robot.emergency()
        robot.land()


def testTakePicture( robot ):
    print("TEST take picture")
    robot.videoEnable()
    for i in xrange(10):
        #print(i,)
        robot.update( cmd=None )
    robot.takePicture()
    for i in xrange(10):
        #print(i,)
        robot.update( cmd=None )

g_testVideoIndex = 0
def videoCallback( data, robot=None, debug=False ):
    global g_testVideoIndex
    g_testVideoIndex += 1
    pass #print("Video", len(data))



def testVideoProcessing( robot ):
    print("TEST video")
    robot.videoCbk = videoCallback
    robot.videoEnable()
    prevVideoIndex = 0
    for i in xrange(400):
        if i % 10 == 0:
            if prevVideoIndex == g_testVideoIndex:
                sys.stderr.write('.')
            else:
                sys.stderr.write('o')
            prevVideoIndex = g_testVideoIndex
        if i == 200:
            print("X")
            robot.update( cmd=movePCMDCmd( False, 0, 0, 0, 0 ) )
        robot.update( cmd=None )

def testVideoRecording( robot ):
    robot.videoEnable()
    for i in xrange(100):
        #print(i,)
        robot.update( cmd=None )
        if robot.time is not None:
            break
    print("START")
    robot.update( cmd=videoRecordingCmd( on=True ) )
    robot.wait( 10.0 )
    print("STOP")
    robot.update( cmd=videoRecordingCmd( on=False ) )
    robot.wait( 2.0 )


def testSpin( robot ):
    "the motors do not spin - the behavior is different to Rolling Spider"
    for i in xrange(10):
        robot.update( cmd=movePCMDCmd( True, 0, 0, 0, 0 ) )
    for i in xrange(10):
        robot.update( cmd=movePCMDCmd( False, 0, 0, 0, 0 ) )


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)
    metalog=None
    if len(sys.argv) > 2:
        metalog = MetaLog( filename=sys.argv[2] )
    if len(sys.argv) > 3 and sys.argv[3] == 'F':
        disableAsserts()

    robot = Bebop( metalog=metalog )
#    testCamera( robot )
#    testEmergency( robot )
#    testTakeoff( robot )
#    testManualControlException( robot )
#    testTakePicture( robot )
#    testFlying( robot )
#    testFlyForward( robot )
    testVideoProcessing( robot )
#    testVideoRecording( robot )
#    testSpin( robot )
    print("Battery:", robot.battery)

# vim: expandtab sw=4 ts=4