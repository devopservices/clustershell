#!/usr/bin/env python
#
# Copyright CEA/DAM/DIF (2010, 2011, 2012)
#  Contributor: Henri DOREAU <henri.doreau@cea.fr>
#  Contributor: Stephane THIELL <stephane.thiell@cea.fr>
#
# This file is part of the ClusterShell library.
#
# This software is governed by the CeCILL-C license under French law and
# abiding by the rules of distribution of free software.  You can  use,
# modify and/ or redistribute the software under the terms of the CeCILL-C
# license as circulated by CEA, CNRS and INRIA at the following URL
# "http://www.cecill.info".
#
# As a counterpart to the access to the source code and  rights to copy,
# modify and redistribute granted by the license, users are provided only
# with a limited warranty  and the software's author,  the holder of the
# economic rights,  and the successive licensors  have only  limited
# liability.
#
# In this respect, the user's attention is drawn to the risks associated
# with loading,  using,  modifying and/or developing or reproducing the
# software by the user in light of its specific status of free software,
# that may mean  that it is complicated to manipulate,  and  that  also
# therefore means  that it is reserved for developers  and  experienced
# professionals having in-depth computer knowledge. Users are therefore
# encouraged to load and test the software's suitability as regards their
# requirements in conditions enabling the security of their systems and/or
# data to be ensured and,  more generally, to use and operate it in the
# same conditions as regards security.
#
# The fact that you are presently reading this means that you have had
# knowledge of the CeCILL-C license and that you accept its terms.

"""
ClusterShell agent launched on remote gateway nodes. This script reads messages
on stdin via the SSH connection, interprets them, takes decisions, and prints
out replies on stdout.
"""

import logging
import os
import sys

from ClusterShell.Event import EventHandler
from ClusterShell.NodeSet import NodeSet
from ClusterShell.Task import task_self, _getshorthostname
from ClusterShell.Engine.Engine import EngineAbortException
from ClusterShell.Worker.fastsubprocess import set_nonblock_flag
from ClusterShell.Worker.Worker import WorkerSimple
from ClusterShell.Worker.Tree import WorkerTree
from ClusterShell.Communication import Channel, ConfigurationMessage, \
    ControlMessage, ACKMessage, ErrorMessage, EndMessage, StdOutMessage, \
    StdErrMessage, RetcodeMessage, TimeoutMessage


class WorkerTreeResponder(EventHandler):
    """Gateway WorkerTree handler"""
    def __init__(self, task, gwchan, srcwkr):
        EventHandler.__init__(self)
        self.gwchan = gwchan    # gateway channel
        self.srcwkr = srcwkr    # id of distant parent WorkerTree
        self.worker = None      # local WorkerTree instance
        # For messages grooming
        qdelay = task.info("grooming_delay")
        self.timer = task.timer(qdelay, self, qdelay, autoclose=True)
        self.logger = logging.getLogger(__name__)
        self.logger.debug("WorkerTreeResponder: initialized")

    def ev_start(self, worker):
        self.logger.debug("WorkerTreeResponder: ev_start")
        self.worker = worker

    def ev_timer(self, timer):
        """perform gateway traffic grooming"""
        if not self.worker:
            return
        logger = self.logger
        # check for grooming opportunities
        for msg_elem, nodes in self.worker.iter_errors():
            logger.debug("iter(stderr): %s: %d bytes" % \
                (nodes, len(msg_elem.message())))
            self.gwchan.send(StdErrMessage(nodes, msg_elem.message(), \
                                           self.srcwkr))
        for msg_elem, nodes in self.worker.iter_buffers():
            logger.debug("iter(stdout): %s: %d bytes" % \
                (nodes, len(msg_elem.message())))
            self.gwchan.send(StdOutMessage(nodes, msg_elem.message(), \
                                           self.srcwkr))
        self.worker.flush_buffers()

    def ev_error(self, worker):
        self.logger.debug("WorkerTreeResponder: ev_error %s" % \
            worker.current_errmsg)

    def ev_timeout(self, worker):
        """Received timeout event: some nodes did timeout"""
        self.gwchan.send(TimeoutMessage( \
            NodeSet._fromlist1(worker.iter_keys_timeout()), self.srcwkr))

    def ev_close(self, worker):
        """End of responder"""
        self.logger.debug("WorkerTreeResponder: ev_close")
        # finalize grooming
        self.ev_timer(None)
        # send retcodes
        for rc, nodes in self.worker.iter_retcodes():
            self.logger.debug("iter(rc): %s: rc=%d" % (nodes, rc))
            self.gwchan.send(RetcodeMessage(nodes, rc, self.srcwkr))
        self.timer.invalidate()
        # clean channel closing
        ####self.gwchan.close()


class GatewayChannel(Channel):
    """high level logic for gateways"""
    def __init__(self, task, hostname):
        """
        """
        Channel.__init__(self)
        self.task = task
        self.hostname = hostname
        self.topology = None
        self.propagation = None
        self.logger = logging.getLogger(__name__)

        self.current_state = None
        self.states = {
            'CFG': self._state_cfg,
            'CTL': self._state_ctl,
            'GTR': self._state_gtr,
        }

    def start(self):
        """initialization"""
        self._open()
        # prepare to receive topology configuration
        self.current_state = self.states['CFG']
        self.logger.debug('entering config state')

    def close(self):
        """close gw channel"""
        self.logger.debug('closing gw channel')
        self._close()
        self.current_state = None

    def recv(self, msg):
        """handle incoming message"""
        try:
            self.logger.debug('handling incoming message: %s', str(msg))
            if msg.ident == EndMessage.ident:
                self.logger.debug('recv: got EndMessage')
                self.worker.abort()
            else:
                self.current_state(msg)
        except Exception, ex:
            self.logger.exception('on recv(): %s', str(ex))
            self.send(ErrorMessage(str(ex)))

    def _state_cfg(self, msg):
        """receive topology configuration"""
        if msg.type == ConfigurationMessage.ident:
            self.topology = msg.data_decode()
            task_self().topology = self.topology
            self.logger.debug('decoded propagation tree')
            self.logger.debug('%s' % str(self.topology))
            self._ack(msg)
            self.current_state = self.states['CTL']
            self.logger.debug('entering control state')
        else:
            logging.error('unexpected message: %s', str(msg))

    def _state_ctl(self, msg):
        """receive control message with actions to perform"""
        if msg.type == ControlMessage.ident:
            self.logger.debug('GatewayChannel._state_ctl')
            self._ack(msg)
            if msg.action == 'shell':
                data = msg.data_decode()
                cmd = data['cmd']
                stderr = data['stderr']
                timeout = data['timeout']

                #self.propagation.invoke_gateway = data['invoke_gateway']
                self.logger.debug('decoded gw invoke (%s)', \
                                  data['invoke_gateway'])

                taskinfo = data['taskinfo']
                task = task_self()
                task._info = taskinfo
                task._engine.info = taskinfo

                #logging.setLevel(logging.DEBUG)

                self.logger.debug('assigning task infos (%s)' % \
                    str(data['taskinfo']))

                self.logger.debug('inherited fanout value=%d', \
                                  task.info("fanout"))

                #self.current_state = self.states['GTR']
                self.logger.debug('launching execution/enter gathering state')

                responder = WorkerTreeResponder(task, self, msg.srcid)

                self.propagation = WorkerTree(msg.target, responder, timeout,
                                              command=cmd,
                                              topology=self.topology,
                                              newroot=self.hostname,
                                              stderr=stderr)
                # FIXME ev_start-not-called workaround
                responder.worker = self.propagation
                self.propagation.upchannel = self
                task.schedule(self.propagation)
                self.logger.debug("WorkerTree scheduled")
            elif msg.action == 'write':
                data = msg.data_decode()
                self.logger.debug('GatewayChannel write: %d bytes', \
                                  len(data['buf']))
                self.propagation.write(data['buf'])
            elif msg.action == 'eof':
                self.logger.debug('GatewayChannel eof')
                self.propagation.set_write_eof()
            else:
                logging.error('unexpected CTL action: %s', msg.action)
        else:
            logging.error('unexpected message: %s', str(msg))

    def _state_gtr(self, msg):
        """gather outputs"""
        # FIXME: state GTR not really used, remove it?
        self.logger.debug('GatewayChannel._state_gtr')
        self.logger.debug('incoming output msg: %s' % str(msg))

    def _ack(self, msg):
        """acknowledge a received message"""
        self.send(ACKMessage(msg.msgid))


def gateway_main():
    """ClusterShell gateway entry point"""
    host = _getshorthostname()
    # configure root logger
    logdir = os.path.expanduser(os.environ.get('CLUSTERSHELL_GW_LOG_DIR', \
                                               '/tmp'))
    loglevel = os.environ.get('CLUSTERSHELL_GW_LOG_LEVEL', 'INFO')
    logging.basicConfig(level=getattr(logging, loglevel.upper(), logging.INFO),
                        format='%(asctime)s %(name)s %(levelname)s %(message)s',
                        filename=os.path.join(logdir, "%s.gw.log" % host))
    logger = logging.getLogger(__name__)
    logger.debug('Starting gateway on %s', host)
    logger.debug("environ=%s" % os.environ)

    set_nonblock_flag(sys.stdin.fileno())
    set_nonblock_flag(sys.stdout.fileno())
    set_nonblock_flag(sys.stderr.fileno())

    task = task_self()
    
    # Pre-enable MsgTree buffering on gateway (not available at runtime - #181)
    task.set_default("stdout_msgtree", True)
    task.set_default("stderr_msgtree", True)

    if sys.stdin.isatty():
        logger.critical('Gateway failure: sys.stdin.isatty() is True')
        sys.exit(1)

    worker = WorkerSimple(sys.stdin, sys.stdout, sys.stderr, None,
                          handler=GatewayChannel(task, host))
    task.schedule(worker)
    logger.debug('Starting task')
    try:
        task.resume()
        logger.debug('Task performed')
    except EngineAbortException, exc:
        pass
    except IOError, exc:
        logger.debug('Broken pipe (%s)' % exc)
        raise
    except Exception, exc:
        logger.exception('Gateway failure: %s' % exc)
    logger.debug('The End')

if __name__ == '__main__':
    __name__ = 'ClusterShell.Gateway'
    # To enable gateway profiling:
    #import cProfile
    #cProfile.run('gateway_main()', '/tmp/gwprof')
    gateway_main()
