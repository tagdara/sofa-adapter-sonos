#!/usr/bin/python3

import sys, os
# Add relative paths for the directory where the adapter is located as well as the parent
sys.path.append(os.path.dirname(__file__))
sys.path.append(os.path.join(os.path.dirname(__file__),'../../base'))

from sofabase import sofabase, adapterbase, configbase
import devices


import requests
import math
import random
from collections import namedtuple
from collections import defaultdict
import xml.etree.ElementTree as et
import time
import json
import asyncio
import aiohttp
import xmltodict
import re

import base64
import logging

import soco
import soco.music_library
import soco.exceptions
from soco.events import event_listener
from operator import itemgetter
import concurrent.futures


class sonos(sofabase):
    
    class adapter_config(configbase):
    
        def adapter_fields(self):
            self.players=self.set_or_default('players', default=[])

  
    class EndpointHealth(devices.EndpointHealth):

        @property            
        def connectivity(self):
            return 'OK'

    class InputController(devices.InputController):

        @property            
        def input(self):
            try:
                player=self.adapter.getPlayer(self.device)
                if player==None or player.group==None:
                    self.log.warning('.! warning - InputController.input could not get a player for %s' % self.device.endpointId)
                    return ""
                #return "sonos:player:%s" % player.group.coordinator.uid
                return player.group.coordinator.player_name
            except:
                self.log.error('!! error getting input (coordinator) for %s' % (self.device.endpointId, self.device), exc_info=True)
            return ""

        async def SelectInput(self, payload, correlationToken=''):
            try:
                player=self.adapter.getPlayer(self.device)
                coordinator=player.group.coordinator
                self.log.debug('.. Changing input for %s/%s from %s to %s' % (player.player_name, player.uid, player.group.coordinator.player_name, payload['input']))
                if payload['input']=='' or payload['input']==player.player_name:
                    player.unjoin()
                else:
                    for otherplayer in self.adapter.players:
                        if (payload['input'].endswith(otherplayer.uid) or otherplayer.player_name==payload['input']) and otherplayer.is_visible:
                            player.join(otherplayer)
                            break

                return self.device.Response(correlationToken)
            except:
                self.log.error('!! Error during SelectInput', exc_info=True)
                self.adapter.connect_needed=True
                return None
                
    class SpeakerController(devices.SpeakerController):

        @property            
        def volume(self):
            return int(self.nativeObject['RenderingControl']['volume']['Master'])

        @property            
        def mute(self):
            return self.nativeObject['RenderingControl']['mute']['Master']=="1"

        async def SetVolume(self, payload, correlationToken=''):
            try:
                self.log.info('-> setting volume on %s to %s' % (self.device, int(payload['volume'])))
                player=self.adapter.getPlayer(self.device)
                player.volume=int(payload['volume'])
                return self.device.Response(correlationToken)
            except:
                self.log.error('!! Error during SetVolume', exc_info=True)
                self.adapter.connect_needed=True
                return None

        async def SetMute(self, payload, correlationToken=''):
            try:
                player=self.adapter.getPlayer(self.device)
                player.mute=payload['mute']
                return self.device.Response(correlationToken)

            except:
                self.log.error('!! Error during SetVolume', exc_info=True)
                self.adapter.connect_needed=True
                return None
                
    class FavoriteController(devices.ModeController):

        @property            
        def mode(self):
            try:
                for fav in self._supportedModes:
                    if self._supportedModes[fav]==self.nativeObject['AVTransport']['enqueued_transport_uri']:
                        return "%s.%s" % (self.name, fav)
            except:
                self.log.error('!! error getting surround mode', exc_info=True)
                return ""

        async def SetMode(self, payload, correlationToken=''):
            try:
                fv=""
                fav=self._supportedModes[payload['mode'].split('.',1)[1]] # Yamaha modes have spaces, so set based on display name
                for ndfav in self.adapter.dataset.nativeDevices['favorite']:
                    if ndfav['uri']==fav:
                        fv=ndfav['item_id']
                        break
                player=await self.adapter.getPlayerOrCoordinator(self.device)
                if 'Play' in await self.adapter.getPlayerActions(player):
                    self.log.info('.. play favorite %s' % fv)
                    player.play_uri(uri=fv)

            except:
                self.adapter.log.error('Error setting mode status %s / %s / %s' % (payload, fav, fv), exc_info=True)
            return {}

    class MusicController(devices.MusicController):

        @property            
        def artist(self):
            try:
                coordinator=self.adapter.getCoordinator(self.device)
                # Images for services like soundcloud do not seem to use the album_art_uri - they populate it with a link that will
                # generate a 404.  In these cases you must get the data from avtransport/enqueued_transport_uri_meta_data
                try:
                    if coordinator['AVTransport']['av_transport_uri_meta_data']['item_id']=='lineinput':
                        return ''
                except:
                    pass

                try:
                    if coordinator['AVTransport']['av_transport_uri_meta_data']['title'].startswith('AirPlay Device:'):
                        return ''
                except:
                    pass

                
                if 'creator' in coordinator['AVTransport']['current_track_meta_data']:
                    return coordinator['AVTransport']['current_track_meta_data']['creator']
                if 'artist' in coordinator['AVTransport']['current_track_meta_data']:
                    return coordinator['AVTransport']['current_track_meta_data']['artist']
                if 'enqueued_transport_uri_meta_data' in coordinator['AVTransport'] and 'creator' in coordinator['AVTransport']['enqueued_transport_uri_meta_data']:
                    if coordinator['AVTransport']['enqueued_transport_uri_meta_data']['creator']:
                        return coordinator['AVTransport']['enqueued_transport_uri_meta_data']['creator']
            except:
                return ""

        @property            
        def title(self):
            try:
                coordinator=self.adapter.getCoordinator(self.device)
                try:
                    if coordinator['AVTransport']['av_transport_uri_meta_data']['item_id']=='lineinput':
                        return 'Line-In'
                except:
                    pass


                # CHEESE 6/13 - Sonos has clearly made some changes to the way they are handling lineinput in their data reporting
                # and the above process no longer works. Now the line-in seems to get labeled "AirPlay Device: (device name)"
                try:
                    if coordinator['AVTransport']['av_transport_uri_meta_data']['title'].startswith('AirPlay Device:'):
                        return 'Line-In'
                except:
                    pass


                
                if coordinator['AVTransport']['current_track_meta_data']['title']:
                    return re.sub("[\(\[].*?[\)\]]", "",coordinator['AVTransport']['current_track_meta_data']['title'])
                if 'enqueued_transport_uri_meta_data' in coordinator['AVTransport'] and 'title' in coordinator['AVTransport']['enqueued_transport_uri_meta_data']:
                    if coordinator['AVTransport']['enqueued_transport_uri_meta_data']['title']:
                        return re.sub("[\(\[].*?[\)\]]", "",coordinator['AVTransport']['enqueued_transport_uri_meta_data']['title'])
            except:
                return ""
       
        @property            
        def album(self):
            try:
                coordinator=self.adapter.getCoordinator(self.device)
                try:
                    if coordinator['AVTransport']['av_transport_uri_meta_data']['item_id']=='lineinput':
                        return ''
                except:
                    pass
                
                try:
                    if coordinator['AVTransport']['av_transport_uri_meta_data']['title'].startswith('AirPlay Device:'):
                        return ''
                except:
                    pass


                
                return coordinator['AVTransport']['current_track_meta_data']['album']
            except:
                return ""
                
        @property            
        def art(self):

            coordinator=self.adapter.getCoordinator(self.device)
            try:
                if coordinator['AVTransport']['av_transport_uri_meta_data']['item_id']=='lineinput':
                    return "/image/sonos/logo"
            except:
                pass
                
            try:
                if coordinator['AVTransport']['av_transport_uri_meta_data']['title'].startswith('AirPlay Device:'):
                    return "/image/sonos/logo"
            except:
                pass
                

            try:
                # Images for services like soundcloud do not seem to use the album_art_uri - they populate it with a link that will
                # generate a 404.  In these cases you must get the data from avtransport/enqueued_transport_uri_meta_data
                if 'enqueued_transport_uri_meta_data' in coordinator['AVTransport'] and 'album_art_uri' in coordinator['AVTransport']['enqueued_transport_uri_meta_data']:
                    if coordinator['AVTransport']['enqueued_transport_uri_meta_data']['album_art_uri']!="":
                        return "/image/sonos/player/%s/AVTransport/enqueued_transport_uri_meta_data/album_art_uri" % (coordinator['speaker']['uid'])
                if 'album' in coordinator['AVTransport']['current_track_meta_data']:
                    if 'album_art_uri' in coordinator['AVTransport']['current_track_meta_data']:
                        return "/image/sonos/player/%s/AVTransport/current_track_meta_data/album_art_uri?album=%s" % (coordinator['speaker']['uid'], coordinator['AVTransport']['current_track_meta_data']['album'])
                    if 'album_art' in coordinator['AVTransport']['current_track_meta_data']:
                        return "/image/sonos/player/%s/AVTransport/current_track_meta_data/album_art?album=%s" % (coordinator['speaker']['uid'], coordinator['AVTransport']['current_track_meta_data']['album'])
                return "/image/sonos/logo"
            except:
                return "/image/sonos/logo"

        @property            
        def url(self):
            try:
                coordinator=self.adapter.getCoordinator(self.device)
                try:
                    if coordinator['AVTransport']['av_transport_uri_meta_data']['item_id']=='lineinput':
                        return "lineinput"
                except:
                    pass

                try:
                    if coordinator['AVTransport']['av_transport_uri_meta_data']['title'].startswith('AirPlay Device:'):
                        return "lineinput"
                except:
                    pass

                return coordinator['AVTransport']['enqueued_transport_uri']
            except:
                return ""

        @property            
        def linked(self):
            try:
                members=[]
                player=self.adapter.getPlayer(self.device)
                for member in self.nativeObject['group']['members']:
                    endpointId="sonos:player:%s" % member
                    if member!=player.uid and endpointId in self.adapter.dataset.localDevices:
                        members.append("sonos:player:%s" % member)
                return members

            except:
                self.log.error('Error getting linked players', exc_info=True)
                return []

        @property            
        def playbackState(self):
            try:
                if self.nativeObject['AVTransport']['transport_state']=='TRANSITIONING':
                    return 'PLAYING'
                else:
                    return self.nativeObject['AVTransport']['transport_state']
            except:
                return 'STOPPED'


        async def Play(self, correlationToken=''):
            try:
                player=await self.adapter.getPlayerOrCoordinator(self.device)
                #player=await self.adapter.getPlayerOrCoordinator(self.device)
                if 'Play' in await self.adapter.getPlayerActions(player):
                    player.play()
                return self.device.Response(correlationToken)
            except:
                self.log.error('!! Error during Play', exc_info=True)
            self.adapter.connect_needed=True
            return self.device.ErrorResponse(correlationToken)


        async def PlayFavorite(self, payload, correlationToken=''):
            try:
                player=await self.adapter.getPlayerOrCoordinator(self.device)
                if 'Play' in await self.adapter.getPlayerActions(player):
                    player.playFavorite(payload['favorite'])
                return self.device.Response(correlationToken)

            except:
                self.log.error('!! Error during Play', exc_info=True)
            self.adapter.connect_needed=True
            return self.device.ErrorResponse(correlationToken)

        async def Pause(self, correlationToken=''):
            try:
                player=await self.adapter.getPlayerOrCoordinator(self.device)
                #player=await self.adapter.getPlayerOrCoordinator(self.device)
                if 'Pause' in await self.adapter.getPlayerActions(player):
                    player.pause()
                return self.device.Response(correlationToken)
            except soco.exceptions.SoCoUPnPException:
                self.log.warning('!! Error during Pause (Soco UPNP Exception - Transition not available)')
            except:
                self.log.error('!! Error during Pause', exc_info=True)
            self.adapter.connect_needed=True
            return self.device.ErrorResponse(correlationToken, error_type="NOT_SUPPORTED_IN_CURRENT_MODE", error_message="Transition not available")
                
        async def Stop(self, correlationToken=''):
            try:
                #player=await self.adapter.getPlayerOrCoordinator(self.device)
                player=await self.adapter.getPlayerOrCoordinator(self.device)
                #self.log.debug('.. Preparing to send stop to %s with available actions %s' % (self.device, self.adapter.getPlayerActions(player)))
                if 'Stop' in await self.adapter.getPlayerActions(player):
                    player.stop()
                return self.device.Response(correlationToken)
            except soco.exceptions.SoCoUPnPException:
                self.log.warning('!! Error during Stop (Soco UPNP Exception - Transition not available)')
            except:
                self.log.error('!! Error during Stop', exc_info=True)
            self.adapter.connect_needed=True
            return self.device.ErrorResponse(correlationToken, error_type="NOT_SUPPORTED_IN_CURRENT_MODE", error_message="Transition not available")
                
        async def Skip(self, correlationToken=''):
            try:
                player=await self.adapter.getPlayerOrCoordinator(self.device)
                #player=await self.adapter.getPlayerOrCoordinator(self.device)
                if 'Next' in await self.adapter.getPlayerActions(player):
                    player.next()
                return self.device.Response(correlationToken)
            except soco.exceptions.SoCoUPnPException:
                self.log.warning('!! Error during Skip (Soco UPNP Exception - Transition not available)')
            except:
                self.log.error('!! Error during Skip', exc_info=True)
            self.adapter.connect_needed=True
            return self.device.ErrorResponse(correlationToken, error_type="NOT_SUPPORTED_IN_CURRENT_MODE", error_message="Transition not available")
                
        async def Previous(self, correlationToken=''):
            try:
                player=await self.adapter.getPlayerOrCoordinator(self.device)
                if 'Previous' in await self.adapter.getPlayerActions(player):
                    player.previous()
                return self.device.Response(correlationToken)
            except soco.exceptions.SoCoUPnPException:
                self.log.warning('!! Error during Previous (Soco UPNP Exception - Transition not available)')
            except:
                self.log.error('!! Error during Previous', exc_info=True)
            self.adapter.connect_needed=True
            return self.device.ErrorResponse(correlationToken, error_type="NOT_SUPPORTED_IN_CURRENT_MODE", error_message="Transition not available")

        async def SelectInput(self, payload, correlationToken=''):
            try:
                player=self.adapter.getPlayer(self.device)
                self.log.info('Changing input for %s: %s' % (player.uid, payload['input']))
                if payload['input']=='':
                    player.unjoin()
                else:
                    for otherplayer in self.players:
                        if otherplayer.uid==payload['input'].split(':')[2]:
                            player.join(otherplayer)
                            break

                return self.device.Response(correlationToken)
            except:
                self.log.error('!! Error during SelectInput', exc_info=True)
            self.adapter.connect_needed=True
            return self.device.ErrorResponse(correlationToken)


    class adapterProcess(adapterbase):
        
        def setSocoLoggers(self, level):
            
            for lg in logging.Logger.manager.loggerDict:
                if lg.startswith('soco'):
                    logging.getLogger(lg).setLevel(level)
              
        
        def __init__(self, log=None, loop=None, dataset=None, notify=None, request=None, config=None, **kwargs):
            self.config=config
            self.dataset=dataset
            self.dataset.nativeDevices['player']={}
            self.dataset.nativeDevices['favorite']={}
            self.log=log
            self.setSocoLoggers(logging.DEBUG)
            self.notify=notify
            self.polltime=.1
            self.subscriptions=[]
            self.artcache={}
            self.artqueue=[]
            self.connect_needed=True
            if not loop:
                self.loop = asyncio.new_event_loop()
            else:
                self.loop=loop
            self.readLightLogoImage()
            self.readDarkLogoImage()
                
        def readLightLogoImage(self):
            sonoslogofile = open(os.path.join(os.path.dirname(__file__),"sonoslogo.png"), "rb")
            self.sonoslogo = sonoslogofile.read()
            self.lightlogo = self.sonoslogo

        def readDarkLogoImage(self):
            try:
                sonoslogofile = open(os.path.join(os.path.dirname(__file__),"sonosdark.png"), "rb")
                self.darklogo = sonoslogofile.read()
            except:
                self.log.error('Error getting dark logo', exc_info=True)
               
                
        async def start(self):
            try:
                self.log.info('.. Starting Sonos')
                await self.startSonosConnection()
                await self.pollSubscriptions()
            except:
                self.log.error('Error starting sonos service',exc_info=True)
                
        async def startSonosConnection(self):
            
            try:
                self.subscriptions=[]
                self.players=await self.sonosDiscovery()
                if self.players:
                    for player in self.players:
                        await self.subscribe_player(player)
                    await self.sonosGetSonosFavorites(self.players[0])
                    self.connect_needed=False
            except:
                self.log.error('Error starting sonos connections',exc_info=True)

            
        async def subscribe_player(self, player):

            try:
                result=True
                if player.is_visible:
                    for subService in ['avTransport','deviceProperties','renderingControl','zoneGroupTopology']:
                        try:
                            newsub=self.subscribeSonos(player,subService)
                            if newsub:
                                self.log.info('++ sonos state subscription: %s/%s' % (player.player_name, newsub.service.service_type))
                                self.subscriptions.append(newsub)
                            else:
                                result=False
                                break
                        except:
                            result=False
                            self.log.error('!! Error subscripting to sonos state: %s/%s' % (player.player_name, subService))
                            break
            except requests.exceptions.ConnectionError:
                self.log.error('!! Error connecting to player: %s' % player)
                result=False
                
            return result
            

        def sonosQuery(self, resmd="", uri=""):
        
            player=self.config.players[0]
            parentsource="MediaRenderer/"
            source="AVTransport"
            command="SetAVTransportURI"
            resmd='<DIDL-Lite xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" xmlns:r="urn:schemas-rinconnetworks-com:metadata-1-0/" xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/"><item id="1006206clibrary%2fplaylists%2f56de4623-3f02-4dc8-8d62-3a580d5325eb%2f%23library_playlist" parentID="10082064library%2fplaylists%2f%23library_playlists" restricted="true"><dc:title>A fantastic raygun</dc:title><upnp:class>object.container.playlistContainer</upnp:class><desc id="cdudn" nameSpace="urn:schemas-rinconnetworks-com:metadata-1-0/">SA_RINCON51463_X_#Svc51463-0-Token</desc></item></DIDL-Lite>'
            uri="x-rincon-cpcontainer:1006206clibrary%2fplaylists%2f56de4623-3f02-4dc8-8d62-3a580d5325eb%2f%23library_playlist"
            payload="<InstanceID>0</InstanceID><CurrentURI>"+uri+"</CurrentURI><CurrentURIMetaData>"+resmd+"</CurrentURIMetaData>"
            port=1400
        
            url="http://"+player+":"+str(port)+"/"+parentsource+source+"/Control"
            template='<s:Envelope s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/" xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body><u:'+command+' xmlns:u="urn:schemas-upnp-org:service:'+source+':1">'+payload+'</u:'+command+'></s:Body></s:Envelope>'
            headers={'SOAPACTION': 'urn:schemas-upnp-org:service:'+source+':1#'+command}
            r = requests.post(url, data=template, headers=headers)
            namespaces = {
                'http://schemas.xmlsoap.org/soap/envelope/': None
            }
            response = dict(xmltodict.parse(r.text, namespaces=namespaces))
            self.log.info('.. sonos raw query '+command+': '+str(response))
            return response

        
        async def sonosDiscovery(self):
        
            try:
                discovered=soco.discover()
                if discovered:
                    discoverlist=list(discovered)
                    self.log.info('.. sonos players: %s' % discoverlist)
                else:
                    discoverlist=None
                    
                if discoverlist==None:
                    discoverlist=[]
                    for playername in self.config.players:
                        try:
                            player=soco.SoCo(playername)
                            try:
                                spinfo=player.get_speaker_info()
                                discoverlist.append(player)
                                self.log.info('Added manual player: %s %s' % (player.player_name, playername))
                            except requests.exceptions.ConnectionError:
                                self.log.error('Error getting info from speaker - removed from discovery: %s' % playername)
                            except:
                                self.log.error('Error getting info from speaker - removed from discovery: %s' % playername, exc_info=True)
                        except:
                            self.log.error('Error discovering Sonos device: %s' % playername, exc_info=True)
                            
                if discoverlist==None:
                    self.log.error('Discover: No sonos devices detected')
                    self.connect_needed=True
                    self.polltime=self.polltime*2
                    if self.polltime<5:
                        self.polltime=5
                    return None
                self.polltime=.1
                for player in discoverlist:
                    try:
                        spinfo=player.get_speaker_info()
                        ginfo=await self.getGroupInfo(player)
                        await self.dataset.ingest({"player": { spinfo["uid"]: { "group": ginfo, "speaker": spinfo, "name":player.player_name, "ip_address":player.ip_address }}})
                        ginfo=player.group
                        
                    except:
                        self.log.error('Error getting speaker info: %s' % player, exc_info=True)
                return discoverlist
            except:
                self.log.error('Error discovering Sonos devices', exc_info=True)

        async def getGroupInfo(self, player):
            
            try:
                members=[]
                pmembers=player.group.members
                for member in pmembers:
                    members.append(member.uid)
                return {"members": members, "coordinator": player.group.coordinator.uid }
            except:
                self.log.error('Error getting group info', exc_info=True)


        async def getGroupUUIDs(self, playerId):
        
            try:
                linkedPlayers=[]
                for player in self.players:
                    if player.player_name==playerId or player.uid==playerId:
                        for linked in player.group:
                            if linked.is_visible:
                                linkedPlayers.append(linked.uid)
                if linkedPlayers:
                    return ','.join(linkedPlayers)
                else:
                    return ''
            except:
                self.log.error('Error getting linked players', exc_info=True)


        async def getGroupName(self, playerId):
        
            try:
                for player in self.players:
                    if player.player_name==playerId or player.uid==playerId:
                        return player.group.short_label       
                return ''
            except:
                self.log.error('Error getting group name', exc_info=True)

            
        async def pollSubscriptions(self):
            
            while self.running:
                if self.connect_needed:
                    await self.startSonosConnection()
                try:
                    for device in list(self.subscriptions):
                        if device.is_subscribed:
                            if not device.events.empty():
                                x=device.events.get(timeout=0.2)
                                update=self.unpackEvent(x)
                                if device.service.service_id=='AVTransport':
                                    # apparently the AVtransport update does not work for radio station data but get_current_track_info will
                                    current_info=device.service.soco.get_current_track_info()
                                    #self.log.info('update: %s' % update)
                                    #self.log.info('gcti: %s' % current_info)
                                    del current_info['metadata']
                                    #self.log.info('UPDATE: %s %s' % (isinstance(update['current_track_meta_data'], str), update))
                                    if update and 'current_track_meta_data' in update:
                                        if isinstance(update['current_track_meta_data'], str):
                                            update['current_track_meta_data']=dict()
                                        for item in current_info:
                                            update['current_track_meta_data'][item]=current_info[item]
                                    try:
                                        path='player/%s/AVTransport/current_track_meta_data/album_art_uri' % device.service.soco.uid
                                        await self.getArt(path, update['current_track_meta_data']['album'], update['current_track_meta_data']['album_art_uri'], self.getPlayerByUID(device.service.soco.uid).ip_address)
                                    except:
                                        pass
                                        #self.log.info('no art in %s' % update, exc_info=True)

                                    
                                if device.service.service_id=='ZoneGroupTopology':
                                    # just do them all and see what's really updated
                                    for player in self.players:
                                        ginfo=await self.getGroupInfo(player)
                                        q=await self.dataset.ingest(list(ginfo['members']), overwriteLevel="/player/%s/group/members" % player.uid )
                                        #ginfo=await self.getGroupInfo(device.service.soco)
                                        #q=await self.dataset.ingest(list(ginfo['members']), overwriteLevel="/player/%s/group/members" % device.service.soco.uid )
                                    try:
                                        if 'zone_group_state' in update:
                                            #self.log.info('.. ZoneGroupTopology update, overwriting previous data: %s ' % update)
                                            short_update=update['zone_group_state']['ZoneGroupState']['ZoneGroups']['ZoneGroup']
                                            #q=await self.dataset.ingest(update, overwriteLevel="/player/%s/ZoneGroupTopology" % device.service.soco.uid )
                                            q=await self.dataset.ingest(short_update, overwriteLevel="/player/%s/ZoneGroupTopology/zone_group_state/ZoneGroupState/ZoneGroups/ZoneGroup'" % device.service.soco.uid )
                                        else:
                                            self.log.debug('.. ignoring ZoneGroupTopology update (no zone_group_state): %s ' % update)
                                    except:
                                        self.log.error('.. error with ZGT update', exc_info=True)
                                else:
                                    self.log.debug('.. update from %s %s %s' % (device.service.soco.uid, device.service.service_id, update) )
                                    q=await self.dataset.ingest({'player': { device.service.soco.uid : { device.service.service_id: update }}})

                        else:
                            self.log.info("Subscription ended: %s" % device.__dict__)
                            self.subscriptions.remove(device)
                            
                    #time.sleep(self.polltime)
                    await asyncio.sleep(self.polltime)
                except:
                    self.log.error('Error polling', exc_info=True)


        def subscribeSonos(self,zone,sonosservice):
            
            try:
                subscription=getattr(zone, sonosservice) 
                #self.log.info('Subscribed to '+zone.player_name+'.'+sonosservice+' for '+str(xsub.timeout))
                return subscription.subscribe(requested_timeout=180, auto_renew=True)
            except:
                self.log.error('Error configuring subscription for %s/%s' % (zone, sonosservice))
                return None


        def unpackEvent(self, event):
            
            try:
                eventVars={}
                for item in event.variables:
                    eventVars[item]=self.didlunpack(event.variables[item])
                    if isinstance(eventVars[item], soco.exceptions.SoCoFault):
                        self.log.info('!! SoCoFault decoding item: %s %s %s' % (item, event.variables[item], eventVars[item].cause))
                        eventVars[item]={}
                    elif str(eventVars[item])[:1]=="<":
                        #self.log.info('Possible XML: %s' % str(eventVars[item]) )
                        try:
                            eventVars[item]=self.etree_to_dict(et.fromstring(str(eventVars[item])))
                        except:
                            self.log.error('Error unpacking event: %s' % str(eventVars[item]), exc_info=True)
                return eventVars
                
                
            except:
                self.log.error('Error unpacking event: %s' % event, exc_info=True)


        async def sonosGetSonosFavorites(self, player=None):

            #{'type': 'instantPlay', 'title': 'A fantastic raygun', 'description': 'Amazon Music Playlist', 'parent_id': 'FV:2', 'item_id': 'FV:2/27', 'album_art_uri': 'https://s3.amazonaws.com/redbird-icons/blue_icon_playlists-80x80.png', 'desc': None, 'favorite_nr': '0', 'resource_meta_data': '<DIDL-Lite xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" xmlns:r="urn:schemas-rinconnetworks-com:metadata-1-0/" xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/"><item id="1006206clibrary%2fplaylists%2f56de4623-3f02-4dc8-8d62-3a580d5325eb%2f%23library_playlist" parentID="10082064library%2fplaylists%2f%23library_playlists" restricted="true"><dc:title>A fantastic raygun</dc:title><upnp:class>object.container.playlistContainer</upnp:class><desc id="cdudn" nameSpace="urn:schemas-rinconnetworks-com:metadata-1-0/">SA_RINCON51463_X_#Svc51463-0-Token</desc></item></DIDL-Lite>', 'resources': [<DidlResource 'x-rincon-cpcontainer:1006206clibrary%2fplaylists%2f56de4623-3f02-4dc8-8d62-3a580d5325eb%2f%23library_playlist' at 0x748733f0>], 'restricted': False}
            try:
                ml=soco.music_library.MusicLibrary(player)
                favorites=[]
                sonosfavorites=ml.get_sonos_favorites()
                #sonosfavorites=player.get_sonos_favorites()
                # this does not currently get the album art
                #self.log.info('fav: %s' % sonosfavorites)
                for fav in sonosfavorites:
                    newfav=fav.__dict__
                    try:
                        #self.log.info('res: %s' % fav.resources[0].uri)
                        newfav['uri']=fav.resources[0].uri
                        newfav['resources']=fav.resources[0].__dict__
                    except:
                        self.log.error('Error deciphering resources', exc_info=True)
                        newfav['resources']={}
                        newfav['uri']=''
                    favorites.append(newfav)
                favorites=sorted(favorites, key=itemgetter('title')) 
                self.log.info('favs: %s' % favorites)
                #self.dataset.listIngest('favorites',favorites)
                await self.dataset.ingest({"favorite":favorites})
                # not sure why this line is here. causes errors, probably just left over
                #self.sonosQuery()
            except:
                self.log.error('Error getting sonos favorites', exc_info=True)


        def etree_to_dict(self, t):
        
            d = {t.tag: {} if t.attrib else None}
            children = list(t)
            if children:
                dd = defaultdict(list)
                for dc in map(self.etree_to_dict, children):
                    for k, v in dc.items():
                        dd[k].append(v)
                d = {t.tag: {k: v[0] if len(v) == 1 else v for k, v in dd.items()}}
            if t.attrib:
                d[t.tag].update(('@' + k, v) for k, v in t.attrib.items())
            if t.text:
                text = t.text.strip()
                if children or t.attrib:
                    if text:
                        d[t.tag]['#text'] = text
                else:
                    d[t.tag] = text
            return d


        def didlunpack(self,didl):
        
            try:
                if str(type(didl)).lower().find('didl')>-1:
                    didl=didl.to_dict() #This should work according to the docs but does not for DidlResource
                    #didl=didl.__dict__
                    for item in didl:
                        #self.log.info('Event var: %s (%s) %s' % (item, type(didl[item]).__name__, didl[item]))
                        if type(didl[item]).__name__ in ['MSTrack']:
                            didl[item]=didl[item].__dict__
                            if 'resources' in didl[item]:
                                didl[item]['resources']=self.didlunpack(didl[item]['resources'])
                        else:
                            didl[item]=self.didlunpack(didl[item])

                    #self.log.info('Unpacked DIDL:'+str(didl))
                elif type(didl)==list:
                    for i, item in enumerate(didl):
                        didl[i]=self.didlunpack(item)
                elif type(didl)==dict:
                    for item in didl:
                        #self.log.info('Event var: %s (%s) %s' % (item, type(didl[item]).__name__, didl[item]))
                        if type(didl[item]).__name__ in ['MSTrack']:
                            didl[item]=didl[item].__dict__
                            if 'resources' in didl[item]:
                                didl[item]['resources']=self.didlunpack(didl[item]['resources'])
                        else:
                            didl[item]=self.didlunpack(didl[item])

                        #didl[item]=self.didlunpack(didl[item])
                elif type(didl).__name__ in ['MSTrack']:
                    didl=didl.__dict__
                    if 'metadata' in didl:
                        didl={**didl, **didl['metadata']}
                    if 'resources' in didl:
                        didl['resources']=self.didlunpack(didl['resources'])

        
                return didl    
            except:
                self.log.error('Error unpacking didl: %s' % didl, exc_info=True)

        def getInputList(self):
            
            try:
                inputlist=[]
                for player in self.players:
                    if player.is_visible:
                        inputlist.append(player.player_name)
                return inputlist
            except:
                self.log.error('Error getting input list', exc_info=True)
                return []

                
        async def addSmartDevice(self, path):
            try:
                if path.split("/")[1]=="player":
                    deviceid=path.split("/")[2]
                    endpointId="%s:%s:%s" % ("sonos","player", path.split("/")[2])
                    nativeObject=self.dataset.nativeDevices['player'][deviceid]
                    if 'name' not in nativeObject:
                        self.log.error('No name in %s %s' % (deviceid, nativeObject))
                        return None
                        
                    if endpointId not in self.dataset.localDevices:
                        if 'RenderingControl' in nativeObject:
                            #if 'ZoneGroupTopology' in nativeObject:
                            device=devices.alexaDevice('sonos/player/%s' % deviceid, nativeObject['name'], displayCategories=["SPEAKER"], adapter=self)
                            device.InputController=sonos.InputController(device=device, inputs=self.getInputList())
                            device.EndpointHealth=sonos.EndpointHealth(device=device)
                            device.MusicController=sonos.MusicController(device=device)
                            # This is not supported due to changes around the way security is implemented on sonos
                            #device.FavoriteController=sonos.FavoriteController('Favorite', device=device, 
                            #    supportedModes=self.getFavoriteList())
                            device.SpeakerController=sonos.SpeakerController(device=device)
                            return self.dataset.add_device(device)
                return None
            except:
                self.log.error('Error defining smart device', exc_info=True)
                return None

        def getFavoriteList(self):
            try:
                favs={}
                for fav in self.dataset.nativeDevices['favorite']:
                    favs[fav['title']]=fav['resources']['uri']

            except:
                self.log.error('!! error parsing favorites from dataset', exc_info=True)
            return favs

        def getPlayer(self, device): 
            try:
                for player in self.players:
                    if 'sonos:player:%s' % player.uid==device.endpointId:
                        return player
                self.log.warning('.! warning - did not find player for %s in %s' % (device.endpointId, self.players))
                return None
            except:
                self.log.error('Error getting player', exc_info=True)
                return None

        def getPlayerByUID(self, uid): 
            try:
                for player in self.players:
                    if player.uid==uid:
                        return player
                return None
            except:
                self.log.error('Error getting player', exc_info=True)
                return None
  
        

        async def getPlayerOrCoordinator(self, device, direct=False):
            
            try:
                dev=self.dataset.getDeviceByEndpointId(device.endpointId)
                try:
                    coord=dev.endpointId
                except AttributeError:
                    self.log.error('!! Player is not available for command: %s %s %s %s.' % (endpointId, controller, command, payload))
                    return None
                try:
                    if direct==False and dev.InputController.input and dev.InputController.input!=dev.friendlyName:
                        coord=self.dataset.getDeviceByFriendlyName(dev.InputController.input).endpointId
                        self.log.info('Setting %s to coordinator instead: %s' % (device.endpointId, dev.InputController.input))
                except:
                    coord=dev.endpointId

                if self.players==None:
                    self.log.error('!! No players are available for command: %s %s %s %s.' % (endpointId, controller, command, payload))
                    return None
                    
                for player in self.players:
                    if 'sonos:player:%s' % player.uid==coord:
                        selected_player=player
                        if selected_player.uid!=selected_player.group.coordinator.uid:
                            for player in self.players:
                                if player.uid==selected_player.group.coordinator.uid:
                                    return player
                        return selected_player
                        
                self.log.info('Could not find device: %s' % coord)
            except soco.exceptions.SoCoSlaveException:
                self.log.error('Error from Soco while trying to issue command to a non-coordinator %s %s', (endpointId, command))
            except soco.exceptions.SoCoUPnPException:
                self.log.error('Error from Soco while trying to issue command %s against possible commands: %s' % (command, actions), exc_info=True)
                self.log.error("It is likely that we have now lost connection, subscriptions are dead, and the adapter needs to be restarted")
                self.connect_needed=True
            except:
                self.log.info('!! Error finding proper device or coordinator for %s' % device, exc_info=True)
            
            return None
            
            
        async def getPlayerActions(self, player):
            try:
                #self.log.info("player: %s" % player)
                #self.log.info("actions: %s" % player.avTransport.GetCurrentTransportActions([('InstanceID', 0)]))
                return player.avTransport.GetCurrentTransportActions([('InstanceID', 0)])['Actions'].split(', ')
            except:
                self.log.error('Could not get available actions for %s' % player.player_name, exc_info=True)
                self.connect_needed=True
            return []
            
        def getPlayerCoordinator(self, player):
            try:
                coord=player.group.coordinator.uid
                return self.dataset.nativeDevices['player'][coord]
            except:
                self.log.error('Error getting coordinator', exc_info=True)
 
        def getCoordinator(self, device):
            try:
                player=self.getPlayer(device)
                if player==None:
                    return None
                if player.group==None:
                    return None
                return self.dataset.nativeDevices['player'][player.group.coordinator.uid]
            except:
                self.log.error('Error getting coordinator', exc_info=True)
            return None
           
            
        async def virtualThumbnail(self, path, client=None, width=None, height=None):
            
            try:
                return await self.virtualImage(path, client=client)
            except:
                self.log.error('Couldnt get art for %s' % playerObject, exc_info=True)
                #return {'name':playerObject['name'], 'id':playerObject['speaker']['uid'], 'image':""}

        async def getArt(self, path, album, url="", ip=""):
            try:
                if path in self.artcache and self.artcache[path]['album']==album:
                    return self.artcache[path]['image']
                    
                if url.find('http')==0:
                    pass
                elif ip:
                    if url.find('/')==0:
                        url='http://'+ip+':1400'+url                    
                    else:
                        url='http://'+ip+':1400/getaa?s=1&u='+url
                else:
                    return self.sonoslogo

                timeout = aiohttp.ClientTimeout(total=10)
                async with aiohttp.ClientSession(timeout=timeout) as client:
                    self.log.info('.. downloading and caching album art: %s' % url)
                    async with client.get(url, timeout=10) as response:
                        result=await response.read()
                        if result:
                            self.artcache[path]={'url':url, 'album': album, 'image':result}
                            #self.log.info('artcache %s' % self.artcache.keys())
                            return result            

            except concurrent.futures._base.TimeoutError:
                self.log.error('.! Attempt to get art from sonos device timed out for %s ' % (path))

            except concurrent.futures._base.CancelledError:
                self.log.error('.! Attempt to get art from sonos device cancelled for %s ' % (path))
                    #self.connect_needed=True               
            except:
                self.log.error('Couldnt get art for %s' % path, exc_info=True)
                #return {'name':playerObject['name'], 'id':playerObject['speaker']['uid'], 'image':""}
                
            return self.sonoslogo

        async def virtualImage(self, path, client=None, width=None, height=None):
            
            try:
                if path=='darklogo':
                    return self.darklogo

                if path=='lightlogo':
                    return self.lightlogo

                if path=='logo':
                    return self.sonoslogo
                    
                playerObject=self.dataset.getObjectFromPath(self.dataset.getObjectPath("/"+path))
                url=self.dataset.getObjectFromPath("/"+path)

                if path in self.artcache:
                    return self.artcache[path]['image']

            except concurrent.futures._base.CancelledError:
                self.log.error('Attempt to get art cancelled for %s %s' % (path,url))
                #self.connect_needed=True
                
            except AttributeError:
                self.log.error('Couldnt get art for %s' % playerObject, exc_info=True)
                self.connect_needed=True
                
            except:
                self.log.error('Couldnt get art for %s' % playerObject, exc_info=True)
                #return {'name':playerObject['name'], 'id':playerObject['speaker']['uid'], 'image':""}
                
            return self.sonoslogo



if __name__ == '__main__':
    adapter=sonos(name='sonos')
    adapter.start()
