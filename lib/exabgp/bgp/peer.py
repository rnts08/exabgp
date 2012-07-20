# encoding: utf-8
"""
peer.py

Created by Thomas Mangin on 2009-08-25.
Copyright (c) 2009-2012 Exa Networks. All rights reserved.
"""

import sys
import time
import traceback

from exabgp.bgp.message import Failure
from exabgp.bgp.message.nop import NOP
from exabgp.bgp.message.open.capability import Capabilities
from exabgp.bgp.message.open.capability.id import CapabilityID
from exabgp.bgp.message.open.capability.negociated import Negociated
from exabgp.bgp.message.update import Update
from exabgp.bgp.message.keepalive import KeepAlive
from exabgp.bgp.message.notification import Notification, Notify
from exabgp.bgp.protocol import Protocol
from exabgp.structure.processes import ProcessError

from exabgp.structure.log import Logger,LazyFormat


# ===================================================================
# We tried to read data when the connection is not established (as it seems select let us do that !)

class NotConnected (Exception):
	pass

# As we can not know if this is our first start or not, this flag is used to
# always make the program act like it was recovering from a failure
# If set to FALSE, no EOR and OPEN Flags set for Restart will be set in the
# OPEN Graceful Restart Capability
FORCE_GRACEFUL = True

# Present a File like interface to socket.socket

class Peer (object):
	# debug hold/keepalive timers
	debug_trace = True			# debug traceback on unexpected exception
	update_time = 3

	def __init__ (self,neighbor,supervisor):
		self.logger = Logger()
		self.supervisor = supervisor
		self.neighbor = neighbor
		# The next restart neighbor definition
		self._neighbor = None
		self.bgp = None

		self._loop = None

		# The peer message should be processed
		self._running = False
		# The peer should restart after a stop
		self._restart = True
		# The peer was restarted (to know what kind of open to send for graceful restart)
		self._restarted = FORCE_GRACEFUL
		self._reset_skip()

		# We want to clear the buffer of unsent routes
		self._clear_routes_buffer = None

		# We have routes following a reload (or we just started)
		self._have_routes = True

		self._asn4 = True

		self._route_parsed = 0L
		self._now = time.time()
		self._next_info = self._now + self.update_time

	def _reset_skip (self):
		# We are currently not skipping connection attempts
		self._skip_time = 0
		# when we can not connect to a peer how many time (in loop) should we back-off
		self._next_skip = 0

	def _more_skip (self):
		self._skip_time = time.time() + self._next_skip
		self._next_skip = int(1+ self._next_skip*1.2)
		if self._next_skip > 60:
			self._next_skip = 60

	def me (self,message):
		return "Peer %15s ASN %-7s %s" % (self.neighbor.peer_address,self.neighbor.peer_as,message)

	def stop (self):
		self._running = False
		self._restart = False
		self._restarted = False
		self._reset_skip()

	def reload (self,routes):
		self.neighbor.set_routes(routes)
		self._have_routes = True
		self._clear_routes_buffer = True
		self._reset_skip()

	def restart (self,restart_neighbor=None):
		# we want to tear down the session and re-establish it
		self._running = False
		self._restart = True
		self._restarted = True
		self._neighbor = restart_neighbor
		self._reset_skip()

	def run (self):
		if self._loop:
			try:
				if self._skip_time > time.time():
					return None
				else:
					return self._loop.next()
			except StopIteration:
				self._loop = None
		elif self._restart:
			# If we are restarting, and the neighbor definition is different, update the neighbor
			if self._neighbor:
				self.neighbor = self._neighbor
				self._neighbor = None
			self._running = True
			self._loop = self._run()
		else:
			self.bgp.close('safety shutdown before unregistering peer, session should already be closed, report if seen in anywhere')
			self.supervisor.unschedule(self)

	def _run (self,max_wait_open=10.0):
		try:
			if self.supervisor.processes.broken(self.neighbor.peer_address):
				# XXX: we should perhaps try to restart the process ??
				self.logger.error('ExaBGP lost the helper process for this peer - stopping','process')
				self._running = False

			self.bgp = Protocol(self)
			self.bgp.connect()

			self._reset_skip()

			# The reload() function is called before we get it and it will set this value we do not want on startup
			self._clear_routes_buffer = False

			#
			# SEND OPEN
			#

			_open = self.bgp.new_open(self._restarted,self._asn4)
			yield None

			#
			# READ OPEN
			#

			start = time.time()
			while True:
				opn = self.bgp.read_open(_open,self.neighbor.peer_address.ip)

				if time.time() - start > max_wait_open:
					self.logger.error(self.me('Waited for an OPEN for too long - killing the session'),'supervisor')
					raise Notify(1,1,'The client took over %s seconds to send the OPEN, closing' % str(max_wait_open))

				# OPEN or NOP
				if opn.TYPE == NOP.TYPE:
					yield None
					continue

				if not opn.capabilities.announced(CapabilityID.FOUR_BYTES_ASN) and _open.asn.asn4():
					self._asn4 = False
					raise Notify(2,0,'peer does not speak ASN4 - restarting in compatibility mode')

				if _open.capabilities.announced(CapabilityID.MULTISESSION_BGP):
					if not opn.capabilities.announced(CapabilityID.MULTISESSION_BGP):
						raise Notify(2,7,'peer does not support MULTISESSION')
					local_sessionid = set(_open.capabilities[CapabilityID.MULTISESSION_BGP])
					remote_sessionid = opn.capabilities[CapabilityID.MULTISESSION_BGP]
					# Empty capability is the same as MultiProtocol (which is what we send)
					if not remote_sessionid:
						remote_sessionid.append(CapabilityID.MULTIPROTOCOL_EXTENSIONS)
					remote_sessionid = set(remote_sessionid)
					# As we only send one MP per session, if the matching fails, we have nothing in common
					if local_sessionid.intersection(remote_sessionid) != local_sessionid:
						raise Notify(2,8,'peer did not reply with the sessionid we sent')
					# We can not collide due to the way we generate the configuration
				yield None
				break

			#
			# SEND KEEPALIVE
			#

			message = self.bgp.new_keepalive(True)
			yield True

			#
			# READ KEEPALIVE
			#

			while True:
				message = self.bgp.read_keepalive()
				# KEEPALIVE or NOP
				if message.TYPE == KeepAlive.TYPE:
					break
				yield None

			#
			# ANNOUNCE TO THE PROCESS BGP IS UP
			#

			self.logger.network('Connected to peer %s' % self.neighbor.name())
			if self.neighbor.peer_updates:
				try:
					for name in self.supervisor.processes.notify(self.neighbor.peer_address):
						self.supervisor.processes.write(name,'neighbor %s up\n' % self.neighbor.peer_address)
				except ProcessError:
					# Can not find any better error code than 6,0 !
					# XXX: We can not restart the program so this will come back again and again - FIX
					# XXX: In the main loop we do exit on this kind of error
					raise Notify(6,0,'ExaBGP Internal error, sorry.')


			#
			# SENDING OUR ROUTING TABLE
			#

			# Dict with for each AFI/SAFI pair if we should announce ADDPATH Path Identifier

			for count in self.bgp.new_update():
				yield True

			if self.bgp.negociated.families:
				self.bgp.new_eors()
			else:
				# If we are not sending an EOR, send a keepalive as soon as when finished
				# So the other routers knows that we have no (more) routes to send ...
				# (is that behaviour documented somewhere ??)
				c,k = self.bgp.new_keepalive(True)

			#
			# MAIN UPDATE LOOP
			#

			seen_update = False
			self._route_parsed = 0L
			while self._running:
				# UPDATE TIME
				self._now = time.time()
				
				#
				# CALCULATE WHEN IS THE NEXT UPDATE FOR THE NUMBER OF ROUTES PARSED DUE
				#
				
				if self._now > self._next_info:
					self._next_info = self._now + self.update_time
					display_update = True
				else:
					display_update = False

				#
				# SEND KEEPALIVES
				#

				c,k = self.bgp.new_keepalive(False)

				if display_update:
					self.logger.timers(self.me('Sending Timer %d second(s) left' % c))

				#
				# READ MESSAGE
				#

				message = self.bgp.read_message()
				if message.TYPE == Update.TYPE:
					seen_update = True
					if self.neighbor.peer_updates:
						proc = self.supervisor.processes
						try:
							for name in proc.notify(self.neighbor.peer_address):
								proc.write(name,'neighbor %s update start\n' % self.neighbor.peer_address)
								for route in message.routes:
									proc.write(name,'neighbor %s %s\n' % (self.neighbor.peer_address,str(route)) )
								proc.write(name,'neighbor %s update end\n' % self.neighbor.peer_address)
						except ProcessError:
							raise Failure('Could not send message(s) to helper program(s) : %s' % message)

				# let's read if we have keepalive before doing the timer check
				c = self.bgp.check_keepalive()

				if display_update:
					self.logger.timers(self.me('Receive Timer %d second(s) left' % c))

				#
				# KEEPALIVE
				#

				if message.TYPE == KeepAlive.TYPE:
					self.logger.message(self.me('<< KEEPALIVE'))
				
				#
				# UPDATE
				#

				elif message.TYPE == Update.TYPE:
					if message.routes:
						self.logger.message(self.me('<< UPDATE'))
						self._route_parsed += len(message.routes)
						if self._route_parsed:
							for route in message.routes:
								self.logger.routes(LazyFormat(self.me(''),str,route))
					else:
						self.logger.message(self.me('<< UPDATE (not parsed)'))

				#
				# NO MESSAGES
				#

				elif message.TYPE not in (NOP.TYPE,):
					 self.logger.message(self.me('<< %d' % ord(message.TYPE)))

				#
				# GIVE INFORMATION ON THE NUMBER OF ROUTES SEEN 
				#

				if seen_update and display_update:
					self.logger.supervisor(self.me('processed %d routes' % self._route_parsed))
					seen_update = False

				#
				# IF WE RELOADED, CLEAR THE BUFFER WE MAY HAVE QUEUED AND NOT YET SENT
				#

				if self._clear_routes_buffer:
					self._clear_routes_buffer = False
					self.bgp.clear_buffer()

				#
				# GIVE INFORMATION ON THE NB OF BUFFERED ROUTES
				#

				nb_pending = self.bgp.buffered()
				if nb_pending:
					self.logger.supervisor(self.me('BUFFERED MESSAGES  (%d)' % nb_pending))
					count = 0

				#
				# SEND UPDATES (NEW OR BUFFERED)
				#

				# If we have reloaded, reset the RIB information 

				if self._have_routes:
					self._have_routes = False
					self.logger.supervisor(self.me('checking for new routes to send'))
				
					for count in self.bgp.new_update():
						yield True

				# emptying the buffer of routes

				elif self.bgp.buffered():
					for count in self.bgp.new_update():
						yield True

				#
				# Go to other Peers
				#

				yield None

			#
			# IF GRACEFUL RESTART, SILENT SHUTDOWN
			#

			if self.neighbor.graceful_restart and opn.capabilities.announced(CapabilityID.GRACEFUL_RESTART):
				self.logger.error('Closing the connection without notification','supervisor')
				self.bgp.close('graceful restarted negociated, closing without sending any notification')
				return

			#
			# NOTIFYING OUR PEER OF THE SHUTDOWN
			#

			raise Notify(6,3)

		#
		# CONNECTION FAILURE, UPDATING TIMERS FOR BACK-OFF
		#

		except NotConnected, e:
			self.logger.error('we can not connect to the peer %s' % str(e),'supervisor')
			self._more_skip()
			self.bgp.clear_buffer()
			try:
				self.bgp.close('could not connect to the peer')
			except Failure:
				pass
			return

		#
		# NOTIFY THE PEER OF AN ERROR
		#

		except Notify,e:
			self.logger.error(self.me('Sending Notification (%d,%d) to peer [%s] %s' % (e.code,e.subcode,str(e),e.data)),'supervisor')
			self.bgp.clear_buffer()
			try:
				self.bgp.new_notification(e)
			except Failure:
				pass
			try:
				self.bgp.close('notification sent (%d,%d) [%s] %s' % (e.code,e.subcode,str(e),e.data))
			except Failure:
				pass
			return

		#
		# THE PEER NOTIFIED US OF AN ERROR
		#

		except Notification, e:
			self.logger.error(self.me('Received Notification (%d,%d) %s' % (e.code,e.subcode,str(e))),'supervisor')
			self.bgp.clear_buffer()
			try:
				self.bgp.close('notification received (%d,%d) %s' % (e.code,e.subcode,str(e)))
			except Failure:
				pass
			return

		#
		# OTHER FAILURES
		#

		except Failure, e:
			self.logger.error(self.me(str(e)),'supervisor')
			self._more_skip()
			self.bgp.clear_buffer()
			try:
				self.bgp.close('failure %s' % str(e))
			except Failure:
				pass
			return

		#
		# UNHANDLED PROBLEMS
		#

		except Exception, e:
			self.logger.error(self.me('UNHANDLED EXCEPTION'),'supervisor')
			self._more_skip()
			self.bgp.clear_buffer()
			if self.debug_trace:
				# should really go to syslog
				traceback.print_exc(file=sys.stdout)
				raise
			else:
				self.logger.error(self.me(str(e)),'supervisor')
			if self.bgp: self.bgp.close('internal problem %s' % str(e))
			return