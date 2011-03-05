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

from google.appengine.ext import db
from google.appengine.ext.db import TransactionFailedError

from google.appengine.datastore import entity_pb

from django.utils import simplejson

#    The maximum number of participants in the event
#    Keep it -1 for unlimited access

MAX_PARTICIPANTS = 20

LATEST_GAMEROOM = 'latest_gameroom'

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


class EfficientModel(db.Model):
    
    mc_version  = db.IntegerProperty(default = 0)
    db_version  = db.IntegerProperty(default = 0)
    created     = db.DateTimeProperty(auto_now_add = True)
    updated     = db.DateTimeProperty(auto_now = True)
    
    @property
    def keyname(self):
        return self.key().name()
    
    @property
    def memcache_key(self):
        raise NotImplementedError
    
    @classmethod
    def from_id(cls, id):
        existing_entity_mc = deserialize_entities(memcache.get(id))
        if existing_entity_mc is None:
            logging.info('Entity %s from memcache is None'%id)
            existing_entity_db = cls.get_by_key_name(id, parent = cls.find_parent(id))
            if existing_entity_db is None:
                logging.info('Dummy Entity created for %s' %id)
                return cls(key_name = unicode(id))
            logging.info('Existing entity found from datastore for %s'%id)
            return existing_entity_db
        logging.info('Existing entity from memcache for %s' %id)
        return existing_entity_mc
    
    @classmethod
    def fetch_from_id(cls, id):
        return cls(key_name = unicode(id))._get()
    
    def _from_memcache(self):
        return deserialize_entities(memcache.get(self.memcache_key))
    
    def _get(self):
        memcached_entity = self._from_memcache()
        if memcached_entity is not None:
            logging.info('memcached entity is not none')
            return memcached_entity
        logging.info('Get or Insert. ')
        return self.get_or_insert(key_name = self.keyname)
    
    def _store(self, force = False):
        self.mc_version += 1 
        memcache.set(self.keyname, serialize_entities(self))
        logging.info('Memcache set for: %s' %self.keyname)
        if self.mc_version - self.db_version >= FAULT_TOLERANCE or force:
            logging.info('sync_to_db started for %s. mc_version: %d, db_version: %d'%(
                                            self.keyname, self.mc_version, self.db_version))
            self.sync_to_db()
        elif self.mc_version < self.db_version:
            logging.info('sync_from_db started')
            self.sync_from_db()
    
    def sync_to_db(self):
        self.db_version = self.mc_version
        db.put(self)
        logging.info('PUTting %s'%self.keyname)
        self._store()

    def sync_from_db(self, **kwargs):
        raise NotImplementedError
        """
        if not classname: return False
        from_ds = db.get(db.Key.from_path(classname, self.keyname))
        logging.info('GETting %s'%self.keyname)
        memcache.set(self.keyname, serialize_entities(from_ds))
        self._store()
        """
           
    @classmethod
    def find_parent(cls, id):
        raise NotImplementedError


class Game(EfficientModel):
    """
        The Model to store the details of a particular
        game room.
    """
    players     = db.StringListProperty()
    chat        = db.TextProperty()
    active      = db.BooleanProperty(default = True)
    channels    = db.StringListProperty()

    deltas = {} 
    
    @property
    def memcache_key(self):
        return 'tournament_' + self.key().name()
    
    @classmethod
    def join_latest_or_new(cls, player):
        latest_roomkey = memcache.get(LATEST_GAMEROOM)
        if latest_roomkey:
            cls.from_id(latest_roomkey).add_player(player)
            return latest_roomkey
        return cls().new(player)
    
    def can_add_player(self):
        if MAX_PARTICIPANTS == -1: return True
        return len(self.players) < MAX_PARTICIPANTS
    
    def add_player(self, player):
        if self.can_add_player():
            logging.info('Adding to existing game')
            if player.keyname not in self.players:
                self.players.append(player.keyname)
                self._store()
            p = Player.get_or_insert(key_name = player.keyname,
                                    parent = self)
            memcache.set(player.keyname, serialize_entities(player))
            self.deltas.update({'new_player' : 1,
                                'name' : player.keyname,})
            self.update_channels(player)
            return self.send_updates()
        else:
            return self.new(player)
    
    @classmethod
    def new(cls, player):
        if not player: return
        logging.info('Creating new gameroom...')
        new_room = cls(key_name = str(time.time()),
                        players = [player.keyname])
        sim_player = Player(key_name = player.keyname,
                            parent = new_room,
                            channels = player.channels)
        db.put([new_room, sim_player])
        memcache.set_multi({LATEST_GAMEROOM  : new_room.key().name(),
                            new_room.memcache_key: serialize_entities(new_room),
                            sim_player.keyname   : serialize_entities(sim_player),
                          })
        new_room.update_channels(sim_player)
        return new_room
    
    @classmethod
    def continue_tournament(cls, player):
        room = cls.from_id(player.gameroom)
        room.update_channels(player)
        
    def update_channels(self, player):
        new_channels = player.get_channels()
        if isinstance(new_channels, list):
            self.channels.extend(new_channels)
        if isinstance(new_channels, str):
            self.channels.append(new_channels)
        self.channels = list(set(self.channels))
        self._store(True)
    
    def send_updates(self):
        message = simplejson.dumps(self.deltas)
        for channel_id in self.channels:
            channel.send_message(channel_id, message)
    
    @classmethod
    def find_parent(cls, id):
        return None

    def update_chat(self, player, message):
        self.deltas = {}
        self.deltas.update({'delta_chat' : message,
                            'player' : player.keyname})
        if self.chat:
            self.chat += '<br />' + self.deltas.get('delta_chat')
        else:
            self.chat = self.deltas.get('delta_chat')
        self._store()
        self.send_updates()

    def end(self):
        "End the game"
        player_keys = [db.Key.from_path('Game', self.keyname, 'Player', x) for x in self.players]
        db_keys = [db.key()]
        db_keys.extend(player_keys)
        memcache_keys = self.players
        memcache_keys.append(self.memcache_key)
        memcache.delete_multi(memcache_keys)
        db.delete(db_keys)
    
    def expel(self, player):
        "Expel player from the game"
        memcache.delete(player.memcache_key)     
        
        def txn(game_obj, player_key, player_id):
            db.delete(player_key)
            game_obj.players.remove(player_id)
            game_obj.put()
        
        db.run_in_transaction(txn, 
                           self, db.Key.from_path('Player', player.keyname, parent = player.get_parent()), 
                           player.keyname)
        self.deltas.update({'expel': 1,
                            'name' : player.keyname})
        self.send_updates()

class Player(EfficientModel):
    """
        Stores information about which player is added into which game
        room. This of course assumes that a player can be in only one
        game room at a time.
    """
    assets     = db.TextProperty(default = '')
    name       = db.StringProperty()
    channels   = db.StringListProperty()
    
    @property
    def memcache_key(self):
        return self.key().name()
    
    @property
    def gameroom(self):
        parent = self.parent()
        if parent:
            return parent.key().name()
        parent = self.get_parent()
        if parent:
            return parent.name()
        logging.info('No parent found for player')
    
    def get_channels(self):
        return self._get().channels
    
    def create_channel(self):
        new_channel = gen_channel(self.keyname)
        self.channels.append(new_channel)
        logging.info('Channel %s created for user: %s' %(new_channel, self.keyname))
        self._store()
        return new_channel
    
    def die(self):
        raise NotImplementedError
    
    def do_action(self, action, **kwargs):
        raise NotImplementedError
    
    @classmethod
    def find_parent(cls, id):
        return Game.all(keys_only = True).filter('players = ', id).get()
    
    def get_parent(self):
        return self.__class__.find_parent(self.keyname)

    def chat(self, message):
        if self.gameroom is not None:
            self._store()
            Game.from_id(self.gameroom).update_chat(self, message)
    
    def leave_tournament(self):
        if self.gameroom is not None:
            Game.from_id(self.gameroom).expel(self)
