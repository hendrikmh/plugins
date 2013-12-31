#!/usr/bin/env python3
# vim: set encoding=utf-8 tabstop=4 softtabstop=4 shiftwidth=4 expandtab
#########################################################################
# Copyright 2013 Marcus Popp                               marcus@popp.mx
#########################################################################
#  This file is part of SmartHome.py.    http://mknx.github.io/smarthome/
#
#  SmartHome.py is free software: you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation, either version 3 of the License, or
#  (at your option) any later version.
#
#  SmartHome.py is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with SmartHome.py. If not, see <http://www.gnu.org/licenses/>.
#########################################################################

import logging
import datetime
import functools
import time
import threading
from urllib.parse import urlparse, parse_qs

logger = logging.getLogger('')


class DbLog():

    _version = 2
    # SQL queries
    # time, item_id, val_str, val_num, val_bool
    _setup = {
      'create_db_log' : "CREATE TABLE log (time INTEGER, item_id INTEGER, val_str TEXT, val_num REAL, val_bool BOOLEAN);",
      'create_db_item' : "CREATE TABLE item (id INTEGER, name varchar(255), time INTEGER, val_str TEXT, val_num REAL, val_bool BOOLEAN);",
      'create_index_log' : "CREATE INDEX log_item_id ON log (item_id);",
      'create_index_item' : "CREATE INDEX item_name ON item (name);"
    }
    styles = ('qmark', 'format', 'numeric')

    def __init__(self, smarthome, db, connect, cycle=10):
        self._sh = smarthome
        self.connected = False
        self._dump_cycle = int(cycle)
        self._buffer = {}
        self._buffer_lock = threading.Lock()

        if type(connect) is not list:
            connect = [connect]

        self._params = {}
        for arg in connect:
           key, sep, value = arg.partition(':')
           for t in int, float, str:
             try:
               v = t(value)
               break
             except:
               pass
           self._params[key] = v

        dbapi = self._sh.dbapi(db)
        self.style = dbapi.paramstyle
        if self.style not in self.styles:
            logger.error("DbLog: Format style {} not supported (only {})".format(self.style, self.styles))
            return

        self._fdb_lock = threading.Lock()
        self._fdb_lock.acquire()
        try:
            self._fdb = dbapi.connect(**self._params)
        except Exception as e:
            logger.error("DbLog: Could not connect to the database: {}".format(e))
            self._fdb_lock.release()
            return
        self.connected = True
        logger.info("DbLog: Connected using {} (using {} style)!".format(db, self.style))

        cur = self._fdb.cursor()
        for n in self._setup:
            try:
                cur.execute(self._setup[n])
            except Exception as e:
                logger.warn("DbLog: Query '{}' failed - maybe exists already: {}".format(n, e))
        cur.close()
        self._fdb_lock.release()
        smarthome.scheduler.add('DbLog dump', self._dump, cycle=self._dump_cycle, prio=5)

    def parse_item(self, item):
        if 'dblog' in item.conf:
            self._buffer[item] = []
            return self.update_item
        else:
            return None

    def run(self):
        self.alive = True

    def stop(self):
        self.alive = False
        self._dump()
        self._fdb_lock.acquire()
        try:
            self._fdb.close()
        except Exception:
            pass
        finally:
            self.connected = False
            self._fdb_lock.release()

    def update_item(self, item, caller=None, source=None, dest=None):
        if item.type() == 'num':
           val_str = None
           val_num = float(item())
           val_bool = None
        elif item.type() == 'bool':
           val_str = None
           val_num = None
           val_bool = bool(item())
        else:
           val_str = str(item())
           val_num = None
           val_bool = None

        self._buffer[item].append((self._timestamp(self._sh.now()), val_str, val_num, val_bool))

    def _datetime(self, ts):
        return datetime.datetime.fromtimestamp(ts / 1000, self._sh.tzinfo())

    def _dump(self):
        if not self.connected:
            pass

        logger.debug('Starting dump')
        for item in self._buffer:
            self._buffer_lock.acquire()
            tuples = self._buffer[item]
            self._buffer[item] = []
            self._buffer_lock.release()

            if len(tuples):
                try:
                    self._fdb_lock.acquire()

                    # Create new item ID
                    id = self._fetchone("SELECT id FROM item where name = ?;", (item.id(),))
                    if id == None:
                        id = self._fetchone("SELECT MAX(id) FROM item;")

                        cur = self._fdb.cursor()
                        self._execute("INSERT INTO item(id, name) VALUES(?,?);", (1 if id[0] == None else id[0]+1, item.id()), cur)
                        id = self._fetchone("SELECT id FROM item where name = ?;", (item.id(),), cur)
                        cur.close()

                    id = id[0]
                    logger.debug('Dumping {}/{} with {} values'.format(item.id(), id, len(tuples)))

                    cur = self._fdb.cursor()
                    for t in tuples:
                        _insert = ( t[0], id, t[1], t[2], t[3] )

                        # time, item_id, val_str, val_num, val_bool
                        self._execute("INSERT INTO log VALUES (?,?,?,?,?);", _insert, cur)

                    t = tuples[-1]
                    _update = ( t[0], t[1], t[2], t[3], id )

                    # time, item_id, val_str, val_num, val_bool
                    self._execute("UPDATE item SET time = ?, val_str = ?, val_num = ?, val_bool = ? WHERE id = ?;", _update, )
                    cur.close()

                    self._fdb.commit()
                except Exception as e:
                    logger.warning("DbLog: problem updating {}: {}".format(item.id(), e))
                finally:
                    self._fdb_lock.release()

    def _execute(self, stmt, params=(), cur=None):
        stmt = self._format(stmt)
        if cur == None:
            c = self._fdb.cursor()
            result = c.execute(stmt, params)
            c.close()
        else:
            result = cur.execute(stmt, params)
        return result

    def _fetchone(self, stmt, params=(), cur=None):
        cur = self._fdb.cursor()
        self._execute(stmt, params, cur)
        result = cur.fetchone()
        cur.close()
        return result

    def _format(self, stmt):
        if self.style == 'qmark':
            return stmt
        elif self.style == 'format':
            return stmt.replace('?', '%s')
        elif self.style == 'numeric':
            cnt = 1
            while '?' in stmt:
                stmt = stmt.replace('?', ':' + str(cnt), 1)
                cnt = cnt + 1
            return stmt

    def _timestamp(self, dt):
        return int(time.mktime(dt.timetuple())) * 1000 + int(dt.microsecond / 1000)