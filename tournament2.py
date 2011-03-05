#!/usr/bin/env python
# -*- coding: utf-8 -*-

__author__ = 'Pranav Prakash < x@pranavprakash.in>'

#    This is a demo application about using the Channel API in
#    Google App Engine. This application does chat and some real
#    actions between a set of users in a room.
#
#    Copyright (C) 2010-2011  Pranav Prakash
#
#    This program is free software: you can redistribute it and/or modify
#    it under the terms of the GNU General Public License as published by
#    the Free Software Foundation, either version 3 of the License, or
#    (at your option) any later version.
#
#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU General Public License for more details.
#   You should have received a copy of the GNU General Public License
#   along with this program.  If not, see <http://www.gnu.org/licenses/>.


import md5
import logging
import time, random

from google.appengine.api import channel
from google.appengine.api import memcache
from google.appengine.api import datastore
from google.appengine.api import taskqueue

from google.appengine.ext import db
from google.appengine.ext.db import TransactionFailedError

from google.appengine.datastore import entity_pb

from django.utils import simplejson

#    The maximum number of participants in the event
#    Keep it -1 for unlimited access

MAX_PARTICIPANTS = 20
MAX_PLAYERS = 5

ROUND_TIME = 30 # how long a round lasts in seconds
MAX_WAIT_TIME = 180 # how long a player shall wait for others to join

LATEST_GAMEROOM = 'latest_gameroom'
POWERUP_ACTIVE_TIME = 3
POWERUP_REFILLS_IN = 5
MAX_CONCURRENT_CHANNEL = 30

#   The maximum difference in revisions acceptable  at any instant
#   between memcached values and the datastore values. The higher it
#   is, the greater catastrophe when memcache goes down, but lesser
#   datastore usage. The lesser it is, the more consistent your datastore
#   and memcache are, and higher datastore operations. 4~6
FAULT_TOLERANCE = 4

def gen_channel(userid):
    seed = userid + str(int(time.time()))
    return md5.md5(seed).hexdigest()

def serialize_entities(models):
    if models is None:
        return None
    elif isinstance(models, db.Model):
        return db.model_to_protobuf(models).Encode()
    else:
        return [db.model_to_protobuf(x).Encode() for x in models]

def deserialize_entities(data):
    if data is None:
        return None
    elif isinstance(data, str):
        return db.model_from_protobuf(entity_pb.EntityProto(data))
    else:
        return [db.model_from_protobuf(entity_pb.EntityProto(x)) for x in data]

class BaseTextMessage (object):
    """
    A wrapper aound text chats
    """
    raw_message = None
    message = None

    def __init__ (self, message=''):
        self.raw_message = message
        self.is_clean = False

    def clean (self):
        """
        Add different checks for cleaning the data out here
        """
        raise NotImplementedError

    def as_html (self):
        """
        Returns the chat text as HTML
        """
        raise NotImplementedError

class Chat (BaseTextMessage):
    def as_html (self):
        return self.raw_message

#   Caching Model
#   http://appengine-cookbook.appspot.com/recipe/models-caching/

_db_get_global_cache = {}

def get (keys, **kwargs):
    keys, multiple = datastore.NormalizeAndTypeCheckKeys (keys)
    getted = db.get([key for key in keys if key not in _db_get_global_cache], **kwargs)
    _db_get_global_cache.update (dict ([(x.key(), x) for x in getted]))
    ret = [_db_get_global_cache[k] for k in keys]
    if multiple:
        return ret
    if len (ret) > 0:
        return ret[0]

def rm (keys):
    keys, _ = datastore.NormalizeAndTypeCheckKeys (keys)
    return [_db_get_global_cache.pop (k) for k in keys if k in _db_get_global_cache]

class GlobalCachingModel(db.Model):

    def put(self):
        ret = super(GlobalCachingModel, self).put()
        rm (self.key())
        return ret
    
    def delete (self):
        rm(self.key())
        return super(CachingModel, self).delete()
    
    @classmethod
    def get_by_key_name(cls, key_names, parent=None, **kwargs):
        try:
            parent = db._coerce_to_key(parent)
        except db.BadKeyError, e:
            raise db.BadArgumentError(str (e))
        rpc = datastore.GetRpcFromKwargs(kwargs)
        key_names, multiple = datastore.NormalizeAndTypeCheck(key_names, basestring)
        keys = [datastore.Key.from_path(cls.kind(), name, parent=parent) for name in key_names]
        if multiple:
            return get(keys)
        else:
            return get(keys[0], rpc=rpc)

#   A modification of the GlobalCachingModel above to include
#   internal versioning of the information and more economical use
#   of datastore calls for read and write

def get2 (keys, **kwargs):
    keys, multiple = datastore.NormalizeAndTypeCheckKeys (keys)
    getted_cache = memcache.get_multi (map (str, keys))
    ret = map (deserialize_entities, getted_cache.values ())
    keys_to_fetch = [key for key in keys if getted_cache.get(key, None) is not None]
    getted_db = db.get(keys_to_fetch)
    memcache_to_set = dict ((k,v) for k,v in zip (map (str,keys_to_fetch), 
                                            map (serialize_entities, getted_db)))
    ret.extend(getted_db)
    memcache.set_multi (memcache_to_set)
    if multiple:
        return ret
    if len (ret) > 0:
        return ret[0]

class GlobalVersionedCachingModel(db.Model):
    """
    The Model uses internal versioning of information with prime focus on very
    high read/writes and consistency.
    Every entity has a datastore's version number information and the version
    number from memcache. When the entity is updated, it happens in memcache
    only and the memcached version number increases. If this number is greater
    than the datastore version number by a certain amount called
    "fault tolerance", then the datastor entity is sync'd with the memcache
    entity.
    """
    
    _db_version = db.IntegerProperty (default=0, required=True)
    _cache_version = db.IntegerProperty (default=0, required=True)
    _fault_tolerance = db.IntegerProperty(default = FAULT_TOLERANCE)
    created = db.DateTimeProperty (auto_now_add=True)
    updated = db.DateTimeProperty (auto_now=True)
    
    @property
    def keyname (self):
        return str (self.key ())

    def remove_from_cache (self, update_db=False):
        """
        Removes the cached instance of the entity. If update_db is True,
        then updates the datastore before removing from cache so that no data
        is lost.
        """
        if update_db:
            self.update_to_db()
        memcache.delete(self.keyname)
    
    def update_to_db (self):
        """
        Updates the current state of the entity from memcache to the datastore
        """
        self._db_version = self._cache_version
        logging.info('About to write into db. Key: %s' %self.keyname)
        self.update_cache ()
        return super (GlobalVersionedCachingModel, self).put ()
    
    def update_cache (self):
        """
        Updates the memacahe for this entity
        """
        memcache.set (self.keyname, serialize_entities (self))

    def put (self):
        self._cache_version += 1
        memcache.set (self.keyname, serialize_entities (self))
        logging.info('Memcache set with version: %d for keyname: %s' %( self._cache_version, self.keyname))
        if self._cache_version - self._db_version >= self._fault_tolerance or \
                                                self._cache_version == 1:
            self.update_to_db ()
    
    def delete (self):
        self.remove_from_cache()
        return super (GlobalVersionedCachingModel, self).delete ()
    
    @classmethod
    def get_by_key_name (cls, key_names, parent=None, **kwargs):
        logging.info ('get_by_key_name: %s' % key_names)
        try:
            parent = db._coerce_to_key (parent)
        except db.BadKeyError, e:
            raise db.BadArgumentError (str (e))
        rpc = datastore.GetRpcFromKwargs (kwargs)
        key_names, multiple = datastore.NormalizeAndTypeCheck (key_names, basestring)
        logging.info(key_names)
        keys = [datastore.Key.from_path (cls.kind (), name, parent=parent) for name in key_names]
        logging.info(keys)
        if multiple:
            return get2 (keys)
        else:
            return get2 (keys[0], rpc=rpc)

class BaseModel(GlobalVersionedCachingModel):
    """
    A Base Model class for all the classes to come
    """
    
    @classmethod
    def from_id(cls, id, smart=True):
        """
        Based on the ID given, this method tries to fetch the entity from the datastore.
        Returns the entity if found. 
        Is smart is true and the entity is not found, creates a dummy object and returns it
        
        The ID could be any property which is unique across the entities, and hence it's
        implementation is left to the derived class
        """
        raise NotImplementedError
 
class BaseGameServer (BaseModel):
    """
    Provides methods and properties for a base game server to which players shall connect
    and perform actions in real time.
    """
    game_type = db.StringProperty ()
    max_players = db.IntegerProperty (default=MAX_PLAYERS)
    max_wait_time = db.IntegerProperty (default=MAX_WAIT_TIME)
    players = db.StringListProperty ()
    chat = db.TextProperty ()
    active = db.BooleanProperty (default=False)
    channels = db.StringListProperty()
    game_round = db.IntegerProperty (default=0)
    round_time = db.IntegerProperty (default=ROUND_TIME)
    
    _delta = {}
    
    @classmethod
    def join_latest_or_new (cls, player):
        """
        When a player attempts to join a game, the server tries to connect him to the latest game room 
        which is yet to be filled. If no empty game rooms are found, then a new game room is created
        for that player and the player waits till someone joins it
        """
        latest_gameroom_id = memcache.get(LATEST_GAMEROOM)
        if latest_gameroom_id:
            return cls.from_id (latest_gameroom_id).add_player (player)
        return cls ().new (player)
    
    def add_player (self, player):
        """
        Adds a player to the gameroom and sends an update on all the channels
        """
        if self.max_players == -1 or len (self.players) < self.max_players:
            if player.keyname not in self.players:
                self.players.append (player.keyname)
                self.put()
            player.put ()
            self._delta.update ({'new_player' : 1,
                                'name' : player.name})
            self.update_channels_from_player (player)
            self.send_updates ()
        else:
            return self.new (player)
    
    @classmethod
    def new (cls, player):
        """
        Creates a new room and adds the player to it
        """
        if not player: return
        new_room = cls (key_name=str (time.time ()),
                        players = [player.keyname])
        new_room.update_channels_from_player (player)
        new_room.put ()
        memcache.set (LATEST_GAMEROOM, new_room.keyname) 
        player.game_in = db.Key(new_room.keyname)
        player.put ()
        new_room.send_updates ()
    
    def update_channels_from_player (self, player):
        """
        Updates the Channel Listing on which the messages needs to be send. The
        channels on which the player is sitting is added to the list
        
        NOTE:
        This method does not PUT, so make sure to call the Put after this method
        has been called, else the changes shall not reflect
        """
        new_channels = player.channels
        if isinstance (new_channels, list):
            self.channels.extend (new_channels)
        if isinstance (new_channels, str):
            self.channels.append (new_channels)
        self.channels = list (set (self.channels))
        return self.channels
    
    def end (self):
        """
        Ends a gameroom. This is done by removing all the associated players and nullifying
        the existance of the room
        """
        raise NotImplementedError
        
    
    def expel (self, player):
        """
        Removes a player from the game room
        """
        raise NotImplementedError
    
    def update_chat (self, player, message):
        """
        Updates the chat message by the player in the game room
        """
        self._delta = {}
        chat_text = Chat (message).as_html ()
        logging.info ('Chat Text: %s' %chat_text)
        self._delta.update ({ 'delta_chat' : chat_text,
                               'player' : player.keyname})
        if self.chat:
            self.chat += chat_text
        else:
            self.chat = chat_text
        self.put ()
        self.send_updates ()
    
    def send_updates (self):
        """
        Sends an update on all the channels associated with the room
        """
        message = simplejson.dumps (self._delta)
        logging.info ('About to send message to %d people' %(
                                        len (self.channels)))
        if len (self.channels) < MAX_CONCURRENT_CHANNEL:
            for channel_id in self.channels:
                try:
                    channel.send_message (channel_id, message)
                except channel.InvalidChannelClientError, e:
                    logging.info (e)
                    pass
        else:
            #   Do an implementation of task queue here so that more than 30
            #   people can also play awesomely
            #   The task is to break the number of people into smaller batches 
            #   and send them update
            logging.info ('More than MAX_CONCURRENT_CHANNEL')
            raise NotImplementedError
            
    
    @classmethod
    def resume (cls, channel=None):
        """
        Sends an update on the given channel(s) about the game. This is done, when let's say
        a user presses F5 whilst in the middle of the game. So, the game has to continue from
        where it was.
        """
        logging.info (player.game_in.key().name())
        room = cls.from_id (player.game_in.keyname)
        room.update_channels_from_player (player)
        room.put ()

def gen_channel (id):
    """
    Generates a token ID
    """
    seed = id+ str (int (time.time ()))
    return md5.md5 (seed).hexdigest ()

class BasePlayer(BaseModel):
    """
    Provides a convenient wrapper class for a player who shall be playing a game
    in the gameroom in a real time environment
    """
    name = db.StringProperty ()
    channels = db.StringListProperty ()
    active = db.BooleanProperty ()
    game_in = db.ReferenceProperty ()
    is_playing = db.BooleanProperty (default=False)
   

    def create_channel(self):
        """
        Creates a channel for the player over which they will communicate using
        Channel API and returns the token details
        """
        new_channel = gen_channel (self.keyname)
        self.channels.append (new_channel)
        self.put ()
        return new_channel
    
    def die(self):
        """
        The player dies and the world forgets him :-(
        """
        raise NotImplementedError
    
    def leave_tournament(self):
        """
        Player leaves the gameroom he was in, at his will
        """
        raise NotImplementedError


class BasePowerup (BaseModel):
    """
    Provides a convenient wrapper for various powerups that might be available in the game
    
    name            : name of the powerup, Health Refill
    type            : type of the powerup, 
    power_factor    : the factor by which this powerup shall be taken
    multiple_usage  : Boolean, if the powerup can be used multiple number of times
    max_usage       : Integer, the maximum number of times a powerup can be used. Is not of much
                      sense when the multiple_usage is False
    acts_on         : String, Where does it acts on, 'health', 'attack', 'defense'
    active_time     : Integer, For how many game rounds this powerup shall remain active. If a power
                      up is supposed to be active for 3 rounds, it should be 3
    refill          : Boolean, if the powerup refills or its one time use only
    refills_in      : Integer, the number of rounds after which the powerup refills itself
    enabled_after   : Integer, the number of rounds after which the powerup is enabled
    """
    name = db.StringProperty (required=True)
    power_type = db.StringProperty ()
    power_factor = db.IntegerProperty ()
    multiple_usage = db.BooleanProperty (default=False)
    max_usage = db.IntegerProperty (default=1)
    acts_on = db.StringProperty ()
    active_time = db.IntegerProperty (default=POWERUP_ACTIVE_TIME)
    refill = db.BooleanProperty (default=True)
    refills_in = db.IntegerProperty (default=POWERUP_REFILLS_IN)
    enabled_after = db.IntegerProperty (default=1)
    

class DeathMatch (BaseGameServer):
    game_type = 'deathmatch'

    @classmethod
    def from_id (cls, id):
        """
        Fetches or Creates a new player based on the ID
        """
        if isinstance (id, str):
            id = str (db.Key.from_path ('DeathMatch', id))
        elif isinstance (id, DeathMatch):
            id = id.keyname
        cached_entity = deserialize_entities (memcache.get (id))
        if cached_entity is None:
            db_entity = cls.get_by_key_name (id)
            if db_entity is None:
                return cls (key_name = unicode (id))
            else:
                return db_entity
        return cached_entity        


class Player(BasePlayer):
    @classmethod
    def from_id (cls, id):
        """
        Fetches or Creates a new player based on the ID
        
        The ID that is passed to the method, is the userid of the
        person, and inside the BaseClasses, we use key to store memcache
        so, we must convert this userid which also is key_name to key
        """
        id = str (db.Key.from_path ('Player', id))
        logging.info ('Attempting to get user with id: %s' % (id))
        cached_entity = deserialize_entities (memcache.get (id))
        if cached_entity is None:
            logging.info ('Not found in memcache ...')
            db_entity = cls.get_by_key_name (id)
            if db_entity is None:
                logging.info ('Not found in DB. Creating dummy')
                return cls (key_name = unicode (id))
            else:
                return db_entity
        logging.info ('Got user from memcache')
        return cached_entity

    def chat (self, message):
        """
        self.is_playing can not be used as the player can chat even before
        the actual game starts
        """
        if self.game_in is not None:
            DeathMatch.from_id (self.game_in).update_chat (self, message)

