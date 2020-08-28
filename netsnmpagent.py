#
# python-netsnmpagent module
# Copyright (c) 2013-2019 Pieter Hollants <pieter@hollants.com>
# Licensed under the GNU Lesser Public License (LGPL) version 3
#
# Main module
#

""" Allows to write net-snmp subagents in Python.

The Python bindings that ship with net-snmp support client operations
only. I fixed a couple of issues in the existing python-agentx module
but eventually decided to write a new module from scratch due to design
issues. For example, it implemented its own handler for registered SNMP
variables, which requires re-doing a lot of stuff which net-snmp
actually takes care of in its API's helpers.

This module, by contrast, concentrates on wrapping the net-snmp C API
for SNMP subagents in an easy manner. """

import sys, os, re, inspect, ctypes, socket, struct
from collections import defaultdict
from netsnmpapi import *
import netsnmpvartypes
from netsnmpvartypes import _VarType


# Helper function courtesy of Alec Thomas and taken from
# http://stackoverflow.com/questions/36932/how-can-i-represent-an-enum-in-python
def enum(*sequential, **named):
	enums = dict(zip(sequential, range(len(sequential))), **named)
	try:
		# Python 2.x
		enums_iterator = enums.iteritems()
	except AttributeError:
		# Python 3.x
		enums_iterator = enums.items()
	enums["Names"] = dict((value, key) for key, value in enums_iterator)
	return type("Enum", (), enums)


# Indicates the status of a netsnmpAgent object
netsnmpAgentStatus = enum(
	"REGISTRATION",  # Unconnected, SNMP object registrations possible
	"FIRSTCONNECT",  # No more registrations, first connection attempt
	"CONNECTFAILED",  # Error connecting to snmpd
	"CONNECTED",  # Connected to a running snmpd instance
	"RECONNECTING",  # Got disconnected, trying to reconnect
)


def _build_callback_handler(callback):
	""" Helper function to create callback handler for the net-snmp API """

	def callback_with_next_handler(handler_p, *args, **kwargs):
		"""
		Since we are injecting our custom callback _before_ the default helper
		handlers provided by net-snmp, we need to ensure that when it finishes,
		it calls the other remaining handlers. This helper function does just
		that, returning early if the custom handler returned with an error.
		"""
		ret = callback(handler_p, *args, **kwargs)
		if ret != SNMP_ERR_NOERROR:
			return ret

		if handler_p[0].next is not None:
			ret = libnsa.netsnmp_call_next_handler(handler_p, *args, **kwargs)

		return ret

	return SNMPNodeHandler(callback_with_next_handler)


def _inject_custom_handler(handler, registration_info):
	"""
	Helper function to inject a custom handler at the top of the callback
	chain for the given registration info
	"""
	custom_handler = libnsa.netsnmp_create_handler(ctypes.c_char_p(b"custom_handler"), handler)

	if libnsa.netsnmp_inject_handler(registration_info, custom_handler) != SNMPERR_SUCCESS:
		raise netsnmpAgentException("Error injecting custom callback handler!")


class netsnmpAgent(object):
	""" Implements an SNMP agent using the net-snmp libraries. """

	def __init__(self, **args):
		"""Initializes a new netsnmpAgent instance.

		"args" is a dictionary that can contain the following
		optional parameters:

		- AgentName     : The agent's name used for registration with net-snmp.
		- MasterSocket  : The transport specification of the AgentX socket of
		                  the running snmpd instance to connect to (see the
		                  "LISTENING ADDRESSES" section in the snmpd(8) manpage).
		                  Change this if you want to use eg. a TCP transport or
		                  access a custom snmpd instance, eg. as shown in
		                  run_simple_agent.sh, or for automatic testing.
		- PersistenceDir: The directory to use to store persistence information.
		                  Change this if you want to use a custom snmpd
		                  instance, eg. for automatic testing.
		- MIBFiles      : A list of filenames of MIBs to be loaded. Required if
		                  the OIDs, for which variables will be registered, do
		                  not belong to standard MIBs and the custom MIBs are not
		                  located in net-snmp's default MIB path
		                  (/usr/share/snmp/mibs).
		- UseMIBFiles   : Whether to use MIB files at all or not. When False,
		                  the parser for MIB files will not be initialized, so
		                  neither system-wide MIB files nor the ones provided
		                  in the MIBFiles argument will be in use.
		- LogHandler    : An optional Python function that will be registered
		                  with net-snmp as a custom log handler. If specified,
		                  this function will be called for every log message
		                  net-snmp itself generates, with parameters as follows:
		                  1. a string indicating the message's priority: one of
		                  "Emergency", "Alert", "Critical", "Error", "Warning",
		                  "Notice", "Info" or "Debug".
		                  2. the actual log message. Note that heading strings
		                  such as "Warning: " and "Error: " will be stripped off
		                  since the priority level is explicitly known and can
		                  be used to prefix the log message, if desired.
		                  Trailing linefeeds will also have been stripped off.
		                  If undefined, log messages will be written to stderr
		                  instead. """

		# Default settings
		defaults = {
			"AgentName"     : os.path.splitext(os.path.basename(sys.argv[0]))[0],
			"MasterSocket"  : None,
			"PersistenceDir": None,
			"UseMIBFiles"   : True,
			"MIBFiles"      : None,
			"LogHandler"    : None,
		}
		for key in defaults:
			setattr(self, key, args.get(key, defaults[key]))
		if self.UseMIBFiles and self.MIBFiles is not None and type(self.MIBFiles) not in (list, tuple):
			self.MIBFiles = (self.MIBFiles,)

		# Initialize status attribute -- until start() is called we will accept
		# SNMP object registrations
		self._status = netsnmpAgentStatus.REGISTRATION

		# Unfortunately net-snmp does not give callers of init_snmp() (used
		# in the start() method) any feedback about success or failure of
		# connection establishment. But for AgentX clients this information is
		# quite essential, thus we need to implement some more or less ugly
		# workarounds.

		# For net-snmp 5.7.x, we can derive success and failure from the log
		# messages it generates. Normally these go to stderr, in the absence
		# of other so-called log handlers. Alas we define a callback function
		# that we will register with net-snmp as a custom log handler later on,
		# hereby effectively gaining access to the desired information.
		def _py_log_handler(majorID, minorID, serverarg, clientarg):
			# "majorID" and "minorID" are the callback IDs with which this
			# callback function was registered. They are useful if the same
			# callback was registered multiple times.
			# Both "serverarg" and "clientarg" are pointers that can be used to
			# convey information from the calling context to the callback
			# function: "serverarg" gets passed individually to every call of
			# snmp_call_callbacks() while "clientarg" was initially passed to
			# snmp_register_callback().

			# In this case, "majorID" and "minorID" are always the same (see the
			# registration code below). "serverarg" needs to be cast back to
			# become a pointer to a "snmp_log_message" C structure (passed by
			# net-snmp's log_handler_callback() in snmplib/snmp_logging.c) while
			# "clientarg" will be None (see the registration code below).
			logmsg = ctypes.cast(serverarg, snmp_log_message_p)

			# Generate textual description of priority level
			priorities = {
				LOG_EMERG: "Emergency",
				LOG_ALERT: "Alert",
				LOG_CRIT: "Critical",
				LOG_ERR: "Error",
				LOG_WARNING: "Warning",
				LOG_NOTICE: "Notice",
				LOG_INFO: "Info",
				LOG_DEBUG: "Debug"
			}
			msgprio = priorities[logmsg.contents.priority]

			# Strip trailing linefeeds and in addition "Warning: " and "Error: "
			# from msgtext as these conditions are already indicated through
			# msgprio
			msgtext = re.sub(
				"^(Warning|Error): *",
				"",
				u(logmsg.contents.msg.rstrip(b"\n"))
			)

			# Intercept log messages related to connection establishment and
			# failure to update the status of this netsnmpAgent object. This is
			# really an ugly hack, introducing a dependency on the particular
			# text of log messages -- hopefully the net-snmp guys won't
			# translate them one day.
			if  msgprio == "Warning" \
			or  msgprio == "Error" \
			and re.match("Failed to .* the agentx master agent.*", msgtext):
				# If this was the first connection attempt, we consider the
				# condition fatal: it is more likely that an invalid
				# "MasterSocket" was specified than that we've got concurrency
				# issues with our agent being erroneously started before snmpd.
				if self._status == netsnmpAgentStatus.FIRSTCONNECT:
					self._status = netsnmpAgentStatus.CONNECTFAILED

					# No need to log this message -- we'll generate our own when
					# throwing a netsnmpAgentException as consequence of the
					# ECONNECT
					return 0

				# Otherwise we'll stay at status RECONNECTING and log net-snmp's
				# message like any other. net-snmp code will keep retrying to
				# connect.
			elif msgprio == "Info" \
			and  re.match("AgentX subagent connected", msgtext):
				self._status = netsnmpAgentStatus.CONNECTED
			elif msgprio == "Info" \
			and  re.match("AgentX master disconnected us.*", msgtext):
				self._status = netsnmpAgentStatus.RECONNECTING

			# If "LogHandler" was defined, call it to take care of logging.
			# Otherwise print all log messages to stderr to resemble net-snmp
			# standard behavior (but add log message's associated priority in
			# plain text as well)
			if self.LogHandler:
				self.LogHandler(msgprio, msgtext)
			else:
				print("[{0}] {1}".format(msgprio, msgtext))

			return 0

		# We defined a Python function that needs a ctypes conversion so it can
		# be called by C code such as net-snmp. That's what SNMPCallback() is
		# used for. However we also need to store the reference in "self" as it
		# will otherwise be lost at the exit of this function so that net-snmp's
		# attempt to call it would end in nirvana...
		self._log_handler = SNMPCallback(_py_log_handler)

		# Now register our custom log handler with majorID SNMP_CALLBACK_LIBRARY
		# and minorID SNMP_CALLBACK_LOGGING.
		if libnsa.snmp_register_callback(
			SNMP_CALLBACK_LIBRARY,
			SNMP_CALLBACK_LOGGING,
			self._log_handler,
			None
		) != SNMPERR_SUCCESS:
			raise netsnmpAgentException(
				"snmp_register_callback() failed for _netsnmp_log_handler!"
			)

		# Finally the net-snmp logging system needs to be told to enable
		# logging through callback functions. This will actually register a
		# NETSNMP_LOGHANDLER_CALLBACK log handler that will call out to any
		# callback functions with the majorID and minorID shown above, such as
		# ours.
		libnsa.snmp_enable_calllog()

		# Unfortunately our custom log handler above is still not enough: in
		# net-snmp 5.4.x there were no "AgentX master disconnected" log
		# messages yet. So we need another workaround to be able to detect
		# disconnects for this release. Both net-snmp 5.4.x and 5.7.x support
		# a callback mechanism using the "majorID" SNMP_CALLBACK_APPLICATION and
		# the "minorID" SNMPD_CALLBACK_INDEX_STOP, which we can abuse for our
		# purposes. Again, we start by defining a callback function.
		def _py_index_stop_callback(majorID, minorID, serverarg, clientarg):
			# For "majorID" and "minorID" see our log handler above.
			# "serverarg" is a disguised pointer to a "netsnmp_session"
			# structure (passed by net-snmp's subagent_open_master_session() and
			# agentx_check_session() in agent/mibgroup/agentx/subagent.c). We
			# can ignore it here since we have a single session only anyway.
			# "clientarg" will be None again (see the registration code below).

			# We only care about SNMPD_CALLBACK_INDEX_STOP as our custom log
			# handler above already took care of all other events.
			if minorID == SNMPD_CALLBACK_INDEX_STOP:
				self._status = netsnmpAgentStatus.RECONNECTING

			return 0

		# Convert it to a C callable function and store its reference
		self._index_stop_callback = SNMPCallback(_py_index_stop_callback)

		# Register it with net-snmp
		if libnsa.snmp_register_callback(
			SNMP_CALLBACK_APPLICATION,
			SNMPD_CALLBACK_INDEX_STOP,
			self._index_stop_callback,
			None
		) != SNMPERR_SUCCESS:
			raise netsnmpAgentException(
				"snmp_register_callback() failed for _netsnmp_index_callback!"
			)

		# No enabling necessary here

		# Make us an AgentX client
		if libnsa.netsnmp_ds_set_boolean(
			NETSNMP_DS_APPLICATION_ID,
			NETSNMP_DS_AGENT_ROLE,
			1
		) != SNMPERR_SUCCESS:
			raise netsnmpAgentException(
				"netsnmp_ds_set_boolean() failed for NETSNMP_DS_AGENT_ROLE!"
			)

		# Use an alternative transport specification to connect to the master?
		# Defaults to "/var/run/agentx/master".
		# (See the "LISTENING ADDRESSES" section in the snmpd(8) manpage)
		if self.MasterSocket:
			if libnsa.netsnmp_ds_set_string(
				NETSNMP_DS_APPLICATION_ID,
				NETSNMP_DS_AGENT_X_SOCKET,
				b(self.MasterSocket)
			) != SNMPERR_SUCCESS:
				raise netsnmpAgentException(
					"netsnmp_ds_set_string() failed for NETSNMP_DS_AGENT_X_SOCKET!"
				)

		# Use an alternative persistence directory?
		if self.PersistenceDir:
			if libnsa.netsnmp_ds_set_string(
				NETSNMP_DS_LIBRARY_ID,
				NETSNMP_DS_LIB_PERSISTENT_DIR,
				b(self.PersistenceDir)
			) != SNMPERR_SUCCESS:
				raise netsnmpAgentException(
					"netsnmp_ds_set_string() failed for NETSNMP_DS_LIB_PERSISTENT_DIR!"
				)

		# Initialize net-snmp library (see netsnmp_agent_api(3))
		if libnsa.init_agent(b(self.AgentName)) != 0:
			raise netsnmpAgentException("init_agent() failed!")

		# Initialize MIB parser
		if self.UseMIBFiles:
			libnsa.netsnmp_init_mib()

		# If MIBFiles were specified (ie. MIBs that can not be found in
		# net-snmp's default MIB directory /usr/share/snmp/mibs), read
		# them in so we can translate OID strings to net-snmp's internal OID
		# format.
		if self.UseMIBFiles and self.MIBFiles:
			for mib in self.MIBFiles:
				if libnsa.read_mib(b(mib)) == 0:
					raise netsnmpAgentException("netsnmp_read_module({0}) " +
					                            "failed!".format(mib))

		# Initialize our SNMP object registry
		self._objs = defaultdict(dict)

		# For each non-private VarType-inheriting class in the netsnmpvartypes
		# module we dynamically define a class wrapper method in our
		# netsnmpAgent class which, besides instantiation, sets up a Net-SNMP
		# watcher for the instance and registers it within our object registry.
		for vartype_cls in [
			m[1]
			for m
			in inspect.getmembers(sys.modules["netsnmpvartypes"])
			if not m[0].startswith("_")
			and inspect.isclass(m[1])
			and issubclass(m[1], netsnmpvartypes._VarType)
		]:
			# Parse the argument specification for the class's __init__ method
			# to get its default for "initval"
			argspec = inspect.getargspec(vartype_cls.__init__)
			default_initval = argspec[3][list(filter(lambda e: e != "self", argspec[0])).index("initval")]

			# Make class wrapper method available in our netsnmpAgent
			# module under the name of the VarType class
			cls_wrapper = self._generateVarTypeClassWrapper(vartype_cls, default_initval)
			setattr(self, vartype_cls.__name__, cls_wrapper)

	def _generateVarTypeClassWrapper(self, vartype_cls, default_initval):

		def _cls_wrapper(initval=default_initval, oidstr=None, writable=True, context="", callback=None):
			# Get instance of VarType-inheriting class
			cls_inst = vartype_cls(initval)

			# If an oidstr has been provided, this is a standalone scalar
			# variable, i.e. it is not used inside a table.
			if oidstr:
				cls_inst._callback_handler = None
				if callback != None:
					# We defined a Python function that needs a ctypes conversion so it can
					# be called by C code such as net-snmp. That's what SNMPNodeHandler() is
					# used for. However we also need to store the reference in "self" as it
					# will otherwise be lost at the exit of this function so that net-snmp's
					# attempt to call it would end in nirvana...
					cls_inst._callback_handler = _build_callback_handler(callback)

				# Prepare the netsnmp_handler_registration structure.
				cls_inst.handler_reginfo = self._prepareRegistration(oidstr, writable)
				cls_inst.handler_reginfo.contents.contextName = b(context)

				# Create the netsnmp_watcher_info structure.
				cls_inst._watcher = libnsX.netsnmp_create_watcher_info(
					cls_inst.cref(),
					cls_inst._data_size,
					cls_inst._asntype,
					cls_inst._watcher_flags
				)

				# Explicitly set netsnmp_watcher_info structure's
				# max_size parameter. netsnmp_create_watcher_info6 would
				# have done that for us but that function was not yet
				# available in net-snmp 5.4.x.
				cls_inst._watcher.contents.max_size = cls_inst._max_size

				# Register handler and watcher with net-snmp.
				result = libnsX.netsnmp_register_watched_scalar(
					cls_inst.handler_reginfo,
					cls_inst._watcher
				)
				if result != 0:
					raise netsnmpAgentException("Error registering variable with net-snmp!")

				if cls_inst._callback_handler is not None:
					_inject_custom_handler(cls_inst._callback_handler, cls_inst.handler_reginfo)

				# Finally, we keep track of all registered SNMP objects for the
				# getRegistered() method.
				self._objs[context][oidstr] = cls_inst

			return cls_inst

		_cls_wrapper.__name__ = vartype_cls.__name__
		_cls_wrapper.__doc__ = vartype_cls.__doc__

		return _cls_wrapper

	def getRawOid(self, mibOid):
		(oid, oid_len) = self._prepareOID(mibOid)
		rawOid = oid2Str(oid, int(oid_len.value))
		return rawOid

	def _prepareOID(self, oidstr):
		""" Convert OID to c_oid type.
		    "oidstr" is the oid to read
		    Return tuple c_oid array and c_size_t length
		"""
		if self.UseMIBFiles:
			# We can't know the length of the internal OID representation
			# beforehand, so we use a MAX_OID_LEN sized buffer for the call to
			# read_objid() below
			oid = (c_oid * MAX_OID_LEN)()
			oid_len = ctypes.c_size_t(MAX_OID_LEN)

			# Let libsnmpagent parse the OID
			if libnsa.read_objid(
				b(oidstr),
				ctypes.cast(ctypes.byref(oid), c_oid_p),
				ctypes.byref(oid_len)
			) == 0:
				raise netsnmpAgentException("read_objid({0}) failed!".format(oidstr))

			# trim trailing zeroes in oid array
			trimOID = c_oid * oid_len.value
			oid = trimOID(*oid[0:oid_len.value])
		else:
			# Interpret the given oidstr as the oid itself.
			try:
				# The error on this line was annoying
				# parts = [c_oid(long(x) if sys.version_info <= (3,) else int(x)) for x in oidstr.split('.')]
				if sys.version_info <= (3,):

					def long(obj):
						return int(obj)

				parts = [c_oid(long(x)) for x in oidstr.split('.')]
			except ValueError:
				raise netsnmpAgentException("Invalid OID (not using MIB): {0}".format(oidstr))

			oid = (c_oid * len(parts))(*parts)
			oid_len = ctypes.c_size_t(len(parts))

		return (oid, oid_len)

	def _prepareRegistration(self, oidstr, writable=True, callback_handler=None):
		""" Prepares the registration of an SNMP object.

		    "oidstr" is the OID to register the object at.
		    "writable" indicates whether "snmpset" is allowed. """

		# Make sure the agent has not been start()ed yet
		if self._status != netsnmpAgentStatus.REGISTRATION:
			raise netsnmpAgentException("Attempt to register SNMP object " \
			                            "after agent has been started!")

		(oid, oid_len) = self._prepareOID(oidstr)

		# Do we allow SNMP SETting to this OID?
		handler_modes = HANDLER_CAN_RWRITE if writable \
		                                   else HANDLER_CAN_RONLY

		# Create the netsnmp_handler_registration structure. It notifies
		# net-snmp that we will be responsible for anything below the given
		# OID. We use this for leaf nodes only, processing of subtrees will be
		# left to net-snmp.
		handler_reginfo = libnsa.netsnmp_create_handler_registration(
			b(oidstr),
			None,  # the callback_handler would go here
			oid,
			oid_len,
			handler_modes
		)

		return handler_reginfo

	def Table(self, oidstr, indexes, columns, counterobj=None, extendable=False, context="", callback=None):
		agent = self

		# Define a Python class to provide access to the table.
		class Table(object):

			def __init__(self, oidstr, idxobjs, coldefs, counterobj, extendable, context):
				# Create a netsnmp_table_data_set structure, representing both
				# the table definition and the data stored inside it. We use the
				# oidstr as table name.
				self._dataset = libnsX.netsnmp_create_table_data_set(
					ctypes.c_char_p(b(oidstr))
				)

				# Define the table row's indexes
				for idxobj in idxobjs:
					libnsX.netsnmp_table_dataset_add_index(
						self._dataset,
						idxobj._asntype
					)

				# Define the table's columns and their default values
				for coldef in coldefs:
					colno = coldef[0]
					defobj = coldef[1]
					writable = coldef[2] if len(coldef) > 2 \
					                     else 0

					result = libnsX.netsnmp_table_set_add_default_row(
						self._dataset,
						colno,
						defobj._asntype,
						writable,
						defobj.cref(),
						defobj._data_size
					)
					if result != SNMPERR_SUCCESS:
						raise netsnmpAgentException(
							"netsnmp_table_set_add_default_row() failed with "
							"error code {0}!".format(result)
						)

				self._callback_handler = None
				if callback != None:
					# We defined a Python function that needs a ctypes conversion so it can
					# be called by C code such as net-snmp. That's what SNMPNodeHandler() is
					# used for. However we also need to store the reference in "self" as it
					# will otherwise be lost at the exit of this function so that net-snmp's
					# attempt to call it would end in nirvana...
					self._callback_handler = _build_callback_handler(callback)

				self._handler_reginfo = agent._prepareRegistration(oidstr, extendable)
				self._handler_reginfo.contents.contextName = b(context)

				# We need to inject the callback_handler before registering the table_data_set
				if self._callback_handler is not None:
					_inject_custom_handler(self._callback_handler, self._handler_reginfo)

				result = libnsX.netsnmp_register_table_data_set(
					self._handler_reginfo,
					self._dataset,
					None
				)
				if result != SNMP_ERR_NOERROR:
					raise netsnmpAgentException(
						"Error code {0} while registering table with "
						"net-snmp!".format(result)
					)

				# Finally, we keep track of all registered SNMP objects for the
				# getRegistered() method.
				agent._objs[context][oidstr] = self

				# If "counterobj" was specified, use it to track the number
				# of table rows
				if counterobj:
					counterobj.update(0)
				self._counterobj = counterobj

			def getRow(self, idxobjs):
				'''Return existing TableRow in Table'''
				return self._getOrAddRow(idxobjs, _getExistingRow=True)

			def addRow(self, idxobjs):
				'''Return a new TableRow in Table'''
				return self._getOrAddRow(idxobjs)

			def _getOrAddRow(self, idxobjs, _getExistingRow=False):
				'''Return a new or existing TableRow in Table.
				This was the original "getRow" method before the addition of "getRow".'''
				dataset = self._dataset

				# Define a Python class to provide access to the table row.
				class TableRow(object):

					def __init__(self, idxobjs):
						# Create the netsnmp_table_set_storage structure for
						# this row.
						self._table_row = libnsX.netsnmp_table_data_set_create_row_from_defaults(
							dataset.contents.default_row
						)

						# Add the indexes
						for idxobj in idxobjs:
							result = libnsa.snmp_varlist_add_variable(
								ctypes.pointer(self._table_row.contents.indexes),
								None,
								0,
								idxobj._asntype,
								idxobj.cref(is_table_index=True),
								idxobj._data_size
							)
							if result == None:
								raise netsnmpAgentException("snmp_varlist_add_variable() failed!")

					@classmethod
					def _fromExistingRow(cls, row):
						if not bool(row):
							raise netsnmpAgentException('Row not found!')

						tableRow = cls.__new__(cls)
						tableRow._table_row = row
						return tableRow

					def setRowCell(self, column, snmpobj):
						result = libnsX.netsnmp_set_row_column(
							self._table_row,
							column,
							snmpobj._asntype,
							snmpobj.cref(),
							snmpobj._data_size
						)
						if result != SNMPERR_SUCCESS:
							raise netsnmpAgentException("netsnmp_set_row_column() failed with error code {0}!".format(result))

				# I don't like this aproach, but without some refactoring, this get's me an existing TableRow
				if _getExistingRow:
					return TableRow._fromExistingRow(self._getRow(idxobjs))

				row = TableRow(idxobjs)

				libnsX.netsnmp_table_dataset_add_row(
					dataset,  # *table
					row._table_row  # row
				)

				if self._counterobj:
					self._counterobj.update(self._counterobj.value() + 1)

				return row

			def value(self):
				# Because tables are more complex than scalar variables, we
				# return a dictionary representing the table's structure and
				# contents instead of a simple string.
				retdict = {}

				# The first entry will contain the defined columns, their types
				# and their defaults, if set. We use array index 0 since it's
				# impossible for SNMP tables to have a row with that index.
				retdict[0] = {}
				col = self._dataset.contents.default_row
				while bool(col):
					retdict[0][int(col.contents.column)] = {}

					asntypes = {
						ASN_INTEGER:    "Integer",
						ASN_OCTET_STR:  "OctetString",
						ASN_IPADDRESS:  "IPAddress",
						ASN_COUNTER:    "Counter32",
						ASN_COUNTER64:  "Counter64",
						ASN_UNSIGNED:   "Unsigned32",
						ASN_TIMETICKS:  "TimeTicks"
					}
					retdict[0][int(col.contents.column)]["type"] = asntypes[col.contents.type]
					if bool(col.contents.data):
						if col.contents.type == ASN_OCTET_STR:
							retdict[0][int(col.contents.column)]["value"] = u(ctypes.string_at(col.contents.data.string, col.contents.data_len))
						elif col.contents.type == ASN_IPADDRESS:
							uint_value = ctypes.cast(
								(ctypes.c_int * 1)(col.contents.data.integer.contents.value),
								ctypes.POINTER(ctypes.c_uint)
							).contents.value
							retdict[0][int(col.contents.column)]["value"] = socket.inet_ntoa(struct.pack("I", uint_value))
						else:
							retdict[0][int(col.contents.column)]["value"] = col.contents.data.integer.contents.value
					col = col.contents.next

				# Next we iterate over the table's rows, creating a dictionary
				# entry for each row after that row's index.
				row = self._dataset.contents.table.contents.first_row
				while bool(row):
					# We want to return the row index in the same way it is
					# shown when using "snmptable", eg. "aa" instead of 2.97.97.
					# This conversion is actually quite complicated (see
					# net-snmp's sprint_realloc_objid() in snmplib/mib.c and
					# get*_table_entries() in apps/snmptable.c for details).
					# All code below assumes eg. that the OID output format was
					# not changed.

					indices = self._getIndices(row)
					# print("in value(), indices={}".format(indices))

					# Finally, iterate over all columns for this row and add
					# stored data, if present
					retdict[indices] = {}
					data = ctypes.cast(row.contents.data, ctypes.POINTER(netsnmp_table_data_set_storage))
					while bool(data):
						if bool(data.contents.data):
							if data.contents.type == ASN_OCTET_STR:
								retdict[indices][int(data.contents.column)] = u(ctypes.string_at(data.contents.data.string, data.contents.data_len))
							elif data.contents.type == ASN_COUNTER64:
								retdict[indices][int(data.contents.column)] = data.contents.data.counter64.contents.value
							elif data.contents.type == ASN_IPADDRESS:
								uint_value = ctypes.cast((ctypes.c_int * 1)(
									data.contents.data.integer.contents.value),
									ctypes.POINTER(ctypes.c_uint)
									).contents.value
								retdict[indices][int(data.contents.column)] = socket.inet_ntoa(struct.pack("I", uint_value))
							else:
								retdict[indices][int(data.contents.column)] = data.contents.data.integer.contents.value
						else:
							retdict[indices] += {}
						data = data.contents.next

					row = row.contents.next

				return retdict

			def clear(self):
				row = self._dataset.contents.table.contents.first_row
				while bool(row):
					nextrow = row.contents.next
					libnsX.netsnmp_table_dataset_remove_and_delete_row(
						self._dataset,
						row
					)
					row = nextrow
				if self._counterobj:
					self._counterobj.update(0)

			# Return the indices of the specified row as a dotted string.
			def _getIndices(self, row):
				if not bool(row):
					return None

				# snprint_objid() below requires a _full_ OID whereas the
				# table row contains only the current row's identifer.
				# Unfortunately, net-snmp does not have a ready function to
				# get the full OID. The following code was modelled after
				# similar code in netsnmp_table_data_build_result().
				fulloid = ctypes.cast(
					ctypes.create_string_buffer(
						MAX_OID_LEN * ctypes.sizeof(c_oid)
					),
					c_oid_p
				)

				# Registered OID
				rootoidlen = self._handler_reginfo.contents.rootoid_len
				for i in range(0, rootoidlen):
					fulloid[i] = self._handler_reginfo.contents.rootoid[i]

				# Entry
				fulloid[rootoidlen] = 1

				# Fake the column number. Unlike the table_data and
				# table_data_set handlers, we do not have one here. No
				# biggie, using a fixed value will do for our purposes as
				# we'll do away with anything left of the first dot below.
				fulloid[rootoidlen + 1] = 2

				# Index data
				indexoidlen = row.contents.index_oid_len
				for i in range(0, indexoidlen):
					fulloid[rootoidlen + 2 + i] = row.contents.index_oid[i]

				# Convert the full OID to its string representation
				oidcstr = ctypes.create_string_buffer(MAX_OID_LEN)
				libnsa.snprint_objid(
					oidcstr,
					MAX_OID_LEN,
					fulloid,
					rootoidlen + 2 + indexoidlen
				)

				# And finally do away with anything left of the first dot
				# so we keep the row index only
				indices = oidcstr.value.split(b".", 1)[1]

				# If it's a string, remove the double quotes. If it's a
				# string containing an integer, make it one
				try:
					indices = int(indices)
				except ValueError:
					indices = u(indices.replace(b'"', b''))

				return indices

			def _getRow(self, indices=[]):
				'''Returns a NET-SNMP row.'''
				if not isinstance(indices, list):
					indices = [indices]
				if isinstance(indices[0], _VarType):
# 					matchStr = 	'.'.join(str(u(x._cvar.value)) for x in indices)
					# FIXME:  make this robust in case index is string or IP
					matchStr = 	'.'.join(str(x._cvar.value) for x in indices)
				else:
					matchStr = 	'.'.join(str(x) for x in indices)

				row = self._dataset.contents.table.contents.first_row
				rowIndices = self._getIndices(row)

				while bool(row) and matchStr != rowIndices:
					row = row.contents.next
					rowIndices = self._getIndices(row)

				return row

			def setRowColumn(self, indices, colIdx, snmpobj):
				'''This method was added to address a table row memory error.
				
				Following agent.start(), an external client may modify the table 
				entries (SNMPSET). If so, the entire row becomes "stale" and 
				subsequent "TableRow1.setRowCell()" to any column in the stored 
				row will cause A Segment violation.
				
				As a workaround, this method was created to traverse rows from 
				the table each time.
				
				However, a better approach in most cases is to use Table.getRow()
				to retrieve a non-correpted TableRow, and then call the original
				TableRow.setRowCell().
				'''
				row = self._getRow(indices)
				if not bool(row):
					raise netsnmpAgentException("setRowColumn() failed to find row for indices {0}!".format(indices))

				result = libnsX.netsnmp_set_row_column(
					row,
					colIdx,
					snmpobj._asntype,
					snmpobj.cref(),
					snmpobj._data_size
				)
				if result != SNMPERR_SUCCESS:
					raise netsnmpAgentException("netsnmp_set_row_column() failed with error code {0}!".format(result))

			def deleteRow(self, indices):
				row = self._getRow(indices)
				if bool(row):
					libnsX.netsnmp_table_dataset_remove_and_delete_row(
						self._dataset,
						row
					)
					if self._counterobj:
						self._counterobj.update(self._counterobj.value() - 1)

		# Return an instance of the just-defined class to the agent
		return Table(oidstr, indexes, columns, counterobj, extendable, context)

	def getContexts(self):
		""" Returns the defined contexts. """

		return self._objs.keys()

	def getRegistered(self, context=""):
		""" Returns a dictionary with the currently registered SNMP objects.

		    Returned is a dictionary objects for the specified "context",
		    which defaults to the default context. """
		myobjs = {}
		try:
			# Python 2.x
			objs_iterator = self._objs[context].iteritems()
		except AttributeError:
			# Python 3.x
			objs_iterator = self._objs[context].items()
		for oidstr, snmpobj in objs_iterator:
			myobjs[oidstr] = {
				"type": type(snmpobj).__name__,
				"value": snmpobj.value()
			}
		return dict(myobjs)

	def start(self):
		""" Starts the agent. Among other things, this means connecting
		    to the master agent, if configured that way. """
		if  self._status != netsnmpAgentStatus.CONNECTED \
		and self._status != netsnmpAgentStatus.RECONNECTING:
			self._status = netsnmpAgentStatus.FIRSTCONNECT
			libnsa.init_snmp(b(self.AgentName))
			if self._status == netsnmpAgentStatus.CONNECTFAILED:
				msg = "Error connecting to snmpd instance at \"{0}\" -- " \
				      "incorrect \"MasterSocket\" or snmpd not running?"
				msg = msg.format(self.MasterSocket)
				raise netsnmpAgentException(msg)

	def check_and_process(self, block=True):
		""" Processes incoming SNMP requests.
		    If optional "block" argument is True (default), the function
		    will block until a SNMP packet is received. """
		return libnsa.agent_check_and_process(int(bool(block)))

	def shutdown(self):
		libnsa.snmp_shutdown(b(self.AgentName))

		# Unfortunately we can't safely call shutdown_agent() for the time
		# being. All net-snmp versions up to and including 5.7.3 are unable
		# to do proper cleanup and cause issues such as double free()s so that
		# one effectively has to rely on the OS to release resources.
		# libnsa.shutdown_agent()

	def send_trap(self, *args, **kwargs):
		'''Send SNMP traps
			To send simple trap:
				send_trap(<trap>,<specific>)
				send_trap(trap=<trap>,specific=<specific>)
			
			Trap data format:
			trapData = [ {'oid':<varOid>,'var':<varValue>,'type':<varType>}, ]
			
			By default net-snmp lib add sysUpTime.instance with agent uptime.
			Optional add 'uptime = <uptime>' to send_trap to use other uptime value

			To send V2 trap:
				send_trap(
					oid = <trapOid>,
					traps = trapData
				)
			To send V3 trap:
				send_trap(
					oid = <trapOID>,
					traps = trapData	,
					context = <context>
				)
		'''

		agent = self

		class snmp_pdu(object):
			""" clas for handling SNMP PDU objects """

			pdu = None

			def __init__(self, pduType=SNMP_MSG_TRAP2):
				"""create PDU object """
				self.pdu = libnsX.snmp_pdu_create(SNMP_MSG_TRAP2)

			def __del__(self):
				""" destructor """
				if self.pdu:
					self.free()

			def free(self):
				""" free PDU struct """
				if self.pdu:
					libnsX.snmp_free_pdu(self.pdu)
					self.pdu = None

			def variables(self):
				""" function variables()
				
				    return netsnmp_variable_list pointer from PDU
				"""
				return self.pdu.contents.variables

			def _humanToASNtype(self, varType):
				""" convert ASN type name to compatible for snmp_add_var()
					type char
					return single char
				"""
				if varType == None:
					varType = '='
				if len(varType) > 1:
					varTypeL = varType.lower()
					if varTypeL[0:3] == 'hex':
						varType = 'x'
					elif varTypeL[0:3] in ('obj', 'oid'):
						varType = 'o'
					elif varTypeL[0:4] == 'uinteger':
						varType = '3'
					elif varTypeL in ('gauge', 'unsigned32'):
						varType = 'u'
					elif varTypeL[0:2] == 'ip':
						varType = 'a'
					elif varTypeL[0:3] == 'dec':
						varType = 'd'
					elif varTypeL == 'counter64':
						varType = 'C'
					elif varTypeL in ('integer', 'integer32'):
						varType = 'i'
					elif varType[0] == 'B':
						varType = 'b'
				return varType[0]

			def add(self, varOID, varData, varType='='):
				"""add OID value to PDU list
					varType can be any of the following chars:"
						i for INTEGER, INTEGER32
						u for UNSIGNED, GAUGE
						3 for UINTEGER
						c for COUNTER, COUNTER32
						C for COUNTER64
						s for STRING,OCTET_STR
						x for HEX STRING
						d for DECIMAL STRING
						n for NULLOBJ
						o for OBJID
						t for TIMETICKS
						a for IPADDRESS
						b for BITS
						= undocumented autodetect feature to get MIB 'SYNTAX' definition
				"""

				varType = self._humanToASNtype(varType)

				(varOid, varOidLen) = agent._prepareOID(varOID)
				ret = 255
				while ret:
					ret = libnsX.snmp_add_var(
							self.pdu,
							ctypes.cast(ctypes.byref(varOid), c_oid_p),
							varOidLen.value,
							ctypes.c_char(b(varType)),
							ctypes.c_char_p(b('{0}'.format(varData)))
					)
					if ret != 0:
						if varType != '=':
							varType = '='
							print("ZDBG: ret={0}".format(ret))
						else:
							break

				return ret

		trap = kwargs.get('trap')
		specific = kwargs.get('specific')
		oid = kwargs.get('oid')
		traps = kwargs.get('traps')
		context = kwargs.get('context')
		uptime = kwargs.get('uptime')

		if oid:
			# send itrap SNMPv2 or SNMPv3
			pdu = snmp_pdu()

			if uptime:
				pdu.add('SNMPv2-MIB::sysUpTime.0', uptime, 't')

			result = pdu.add('SNMPv2-MIB::snmpTrapOID.0', oid)
			if result != 0:
				msg = "Failed to add {0} as snmpTrapOID!".format(oid)
				raise netsnmpAgentException(msg)
			else:
				# add traps
				if traps == None:
					traps = []
				for entry in traps:
					if ('oid' in entry):
						varOid = entry['oid']
						if ('val' in entry):
							varData = entry['val']
							varType = None
							if ('type' in entry):
								varType = entry['type']
							pdu.add(varOid, varData, varType)
						else:
							msg = "missing 'val' key in trap list!"
							raise netsnmpAgentException(msg)
					else:
						msg = "missing 'oid' key in trap list!"
						raise netsnmpAgentException(msg)

				variables = pdu.variables()
				if context:
					# SNMPv3 trap have context
					# WARNING - I replaced commented code... I believe it's correct
					# ##context = ctype.c_char_p(context)
					# ##libnsa.send_v3trap(variables, context)
					libnsa.send_v3trap(variables, b(context))
				else:
					# SNMPv2 trap
					libnsa.send_v2trap(variables)
		else:
			# send easy trap
			if trap and specific:
				trap = ctypes.c_int(trap)
				specific = ctypes.c_int(specific)
			elif len(args) == 2 and type(args[0]) == int and type(args[1]) == int:
				trap = ctypes.c_int(args[0])
				specific = ctypes.c_int(args[1])
			libnsa.send_easy_trap(trap, specific)


class netsnmpAgentException(Exception):
	pass
