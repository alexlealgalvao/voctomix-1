#!/usr/bin/env python3
import logging

from configparser import NoOptionError
from enum import Enum, unique
import gi
gi.require_version('GstController', '1.0')
from gi.repository import Gst
from lib.config import Config
from vocto.transitions import Composites, Transitions, Frame
from lib.scene import Scene
from lib.overlay import Overlay

from vocto.composite_commands import CompositeCommand


class VideoMix(object):
    log = logging.getLogger('VideoMix')

    def __init__(self):
        # read sources from confg file
        self.sources = Config.getSources()
        self.log.info('Configuring Mixer for %u Sources', len(self.sources))

        # load composites from config
        self.log.info("Reading transitions configuration...")
        self.composites = Config.getComposites()

        # load transitions from configuration
        self.transitions = Config.getTransitions(self.composites)
        self.scene = None
        self.overlay = None

        Config.getAudioStreams()

        # build GStreamer mixing pipeline descriptor
        self.bin = """
            bin.(
                name=VideoMix

                compositor
                    name=videomixer
            """
        if Config.hasOverlay():
            self.bin += """\
                ! queue
                    name=queue-overlay
                ! gdkpixbufoverlay
                    name=overlay
                    overlay-width={width}
                    overlay-height={height}
                """.format(
                width=Config.getVideoResolution()[0],
                height=Config.getVideoResolution()[1]
            )
            if Config.getOverlayFile():
                self.bin += """\
                    location={overlay}
                    alpha=1.0
                    """.format(overlay=Config.getOverlayFilePath(Config.getOverlayFile()))
            else:
                self.log.info("No initial overlay source configured.")

        self.bin += """\
            ! identity
                name=sig
            ! {vcaps}
            ! tee
                name=video-mix

            video-background.
            ! queue
                name=queue-video-background
            ! videomixer.
            """.format(
            vcaps=Config.getVideoCaps()
        )

        for idx, name in enumerate(self.sources):
            self.bin += """
                video-{name}.
                ! queue
                    name=queue-cropper-{name}
                ! videobox
                    name=cropper-{name}
                ! videomixer.
                """.format(
                name=name,
                idx=idx
            )

        self.bin += """)
                    """

    def attach(self, pipeline):
        self.log.debug('Binding Handoff-Handler for '
                       'Synchronus mixer manipulation')
        self.pipeline = pipeline
        sig = pipeline.get_by_name('sig')
        sig.connect('handoff', self.on_handoff)

        self.log.debug('Initializing Mixer-State')
        # initialize pipeline bindings for all sources
        self.scene = Scene(self.sources, pipeline, self.transitions.fps, 1)
        self.compositeMode = None
        self.sourceA = None
        self.sourceB = None
        self.setCompositeEx(Composites.targets(self.composites)[
                            0].name, self.sources[0], self.sources[1])

        if Config.hasOverlay():
            self.overlay = Overlay(
                pipeline, Config.getOverlayFile(), Config.getOverlayBlendTime())

        bgMixerpad = (pipeline.get_by_name('videomixer')
                      .get_static_pad('sink_0'))
        bgMixerpad.set_property('zorder', 0)

    def __str__(self):
        return 'VideoMix'

    def getPlayTime(self):
        # get play time from mixing pipeline or assume zero
        return self.pipeline.get_pipeline_clock().get_time() - \
            self.pipeline.get_base_time()

    def on_handoff(self, object, buffer):
        # sync with self.launch()
        if self.scene and self.scene.dirty:
            # push scene to gstreamer
            playTime = self.getPlayTime()
            self.log.debug('Applying new Mixer-State at %d ms',
                           playTime / Gst.MSECOND)
            self.scene.push(playTime)

    def setCompositeEx(self, newCompositeName=None, newA=None, newB=None, useTransitions=False, dry=False):
        # expect strings or None as parameters
        assert not newCompositeName or type(newCompositeName) == str
        assert not newA or type(newA) == str
        assert not newB or type(newB) == str

        # get current composite
        if not self.compositeMode:
            curCompositeName = None
            self.log.info("Request composite %s(%s,%s)",
                          newCompositeName, newA, newB)
        else:
            curCompositeName = self.compositeMode
            curA = self.sourceA
            curB = self.sourceB
            self.log.info("Request composite change from %s(%s,%s) to %s(%s,%s)",
                          curCompositeName, curA, curB, newCompositeName, newA, newB)

        # check if there is any None parameter and fill it up with
        # reasonable value from the current scene
        if curCompositeName and not (newCompositeName and newA and newB):
            # use current state if not defined by parameter
            if not newCompositeName:
                newCompositeName = curCompositeName
            if not newA:
                newA = curA if newB != curA else curB
            if not newB:
                newB = curA if newA == curB else curB
            self.log.debug("Completing wildcarded composite to %s(%s,%s)",
                           newCompositeName, newA, newB)
        # post condition: we should have all parameters now
        assert newA != newB
        assert newCompositeName and newA and newB

        # fetch composites
        curComposite = self.composites[curCompositeName] if curCompositeName else None
        newComposite = self.composites[newCompositeName]

        # if new scene is complete
        if newComposite and newA in self.sources and newB in self.sources:
            self.log.debug("New composite shall be %s(%s,%s)",
                          newComposite.name, newA, newB)
            # try to find a matching transition from current to new scene
            transition = None
            targetA, targetB = newA, newB
            if useTransitions:
                if curComposite:
                    old = (curA,curB,newA,newB)

                    # check if whe have a three-channel scenario
                    if len(set(old)) == 3:
                        self.log.debug("Current composite includes three different frames: (%s,%s) -> (%s,%s)", *old)
                        # check if current composite hides B
                        if curComposite.single():
                            self.log.debug("Current composite hides channel B so we can secretly change it.")
                            # check for (A,B) -> (A,C)
                            if curA == newA:
                                # change into (A,C) -> (A,C)
                                curB = newB
                            # check for (A,B) -> (C,A)
                            elif curA == newB:
                                # change into (A,C) -> (C,A)
                                curB = newA
                            # check another case where new composite also hides B
                            elif newComposite.single():
                                self.log.debug("New composite also hides channel B so we can secretly change it.")
                                # change (A,B) -> (C,B) into (A,C) -> (C,A)
                                newB = curA
                                curB = newA
                        elif newComposite.single():
                            # check for (A,B) -> (A,C)
                            if curA == newA:
                                newB = curB
                            # check for (A,B) -> (B,C)
                            if curB == newA:
                                newB = curA

                    # check if whe have a four-channel scenario
                    if len(set(old)) == 4:
                        self.log.debug("Current composite includes four different frames: (%s,%s) -> (%s,%s)", *old)
                        # check if both composites hide channel B
                        if curComposite.single() and newComposite.single():
                            self.log.debug("Current and new composite hide channel B so we can secretly change it.")
                            # change (A,B) -> (C,D) into (A,C) -> (C,A)
                            curB = newA
                            newB = curA

                    # log if whe changed somtehing
                    if old != (curA,curB,newA,newB):
                        self.log.info("Changing requested transition from (%s,%s) -> (%s,%s) to (%s,%s) -> (%s,%s)", *old, curA,curB,newA,newB)

                    swap = False
                    if (curA, curB) == (newA, newB) and curComposite != newComposite:
                        transition, swap = self.transitions.solve(
                            curComposite, newComposite, False)
                    elif (curA, curB) == (newB, newA):
                        transition, swap = self.transitions.solve(
                            curComposite, newComposite, True)
                        if not swap:
                            targetA, targetB = newB, newA
                    if transition and not dry:
                        self.log.warning("No transition found")
            if dry:
                return (newA, newB) if transition else False
            if transition:
                # apply found transition
                self.log.debug(
                    "committing transition '%s' to scene", transition.name())
                self.scene.commit(targetA, transition.Az(1, 2))
                self.scene.commit(targetB, transition.Bz(2, 1))
            else:
                # apply new scene (hard cut)
                self.log.debug(
                    "setting composite '%s' to scene", newComposite.name)
                self.scene.set(targetA, newComposite.Az(1))
                self.scene.set(targetB, newComposite.Bz(2))
            # make all other sources invisible
            for source in self.sources:
                if source not in [targetA, targetB]:
                    self.log.debug("making source %s invisible", source)
                    self.scene.set(source, Frame(True, alpha=0, zorder=-1))
        else:
            # report unknown elements of the target scene
            if not newComposite:
                self.log.error("Unknown composite '%s'", newCompositeName)
            if not newA in self.sources:
                self.log.error("Unknown source '%s'", newA)
            if not newB in self.sources:
                self.log.error("Unknown source '%s'", newB)

        # remember scene we've set
        self.compositeMode = newComposite.name
        self.sourceA = newA
        self.sourceB = newB

    def setComposite(self, command, useTransitions=False):
        ''' parse switch to the composite described by string command '''
        # expect string as parameter
        assert type(command) == str
        # parse command
        command = CompositeCommand.from_str(command)
        self.log.debug("Setting new composite by string '%s'", command)
        self.setCompositeEx(command.composite, command.A,
                            command.B, useTransitions)

    def testCut(self, command):
        # expect string as parameter
        assert type(command) == str
        # parse command
        command = CompositeCommand.from_str(command)
        if (command.composite != self.compositeMode or command.A != self.sourceA or command.B != self.sourceB):
            return command.A, command.B
        else:
            return False

    def testTransition(self, command):
        # expect string as parameter
        assert type(command) == str
        # parse command
        command = CompositeCommand.from_str(command)
        self.log.debug("Testing if transition is available to '%s'", command)
        return self.setCompositeEx(command.composite, command.A,
                                   command.B, True, True)

    def getVideoSources(self):
        ''' legacy command '''
        return [self.sourceA, self.sourceB]

    def setVideoSourceA(self, source):
        ''' legacy command '''
        setCompositeEx(None, source, None, useTransitions=False)

    def getVideoSourceA(self):
        ''' legacy command '''
        return self.sourceA

    def setVideoSourceB(self, source):
        ''' legacy command '''
        setCompositeEx(None, None, source, useTransitions=False)

    def getVideoSourceB(self):
        ''' legacy command '''
        return self.sourceB

    def setCompositeMode(self, mode):
        ''' legacy command '''
        setCompositeEx(mode, None, None, useTransitions=False)

    def getCompositeMode(self):
        ''' legacy command '''
        return self.compositeMode

    def getComposite(self):
        ''' legacy command '''
        return str(CompositeCommand(self.compositeMode, self.sourceA, self.sourceB))

    def setOverlay(self, location):
        ''' set up overlay file by location '''
        self.overlay.set(location)

    def showOverlay(self, visible):
        ''' set overlay visibility '''
        self.overlay.show(visible, self.getPlayTime())

    def getOverlay(self):
        ''' get current overlay file location '''
        return self.overlay.get()

    def getOverlayVisible(self):
        ''' get overlay visibility '''
        return self.overlay.visible()
