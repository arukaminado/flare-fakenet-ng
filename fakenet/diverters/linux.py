import sys
import dpkt
import time
import socket
import logging
import traceback
import threading
import subprocess
import netfilterqueue
from linutil import *
from . import fnpacket
from debuglevels import *
from diverterbase import *
from collections import namedtuple
from netfilterqueue import NetfilterQueue


class LinuxPacketCtx(fnpacket.PacketCtx):
    def __init__(self, lbl, nfqpkt):
        self.nfqpkt = nfqpkt
        raw = nfqpkt.get_payload()

        super(LinuxPacketCtx, self).__init__(lbl, raw)


class Diverter(DiverterBase, LinUtilMixin):

    def __init__(self, diverter_config, listeners_config, ip_addrs,
                 logging_level=logging.INFO):
        super(Diverter, self).__init__(diverter_config, listeners_config,
                                       ip_addrs, logging_level)

        self.init_linux_mixin()
        self.init_diverter_linux()

    def init_diverter_linux(self):
        """Linux-specific Diverter initialization."""

        self.logger.info('Running in %s mode' % (self.network_mode))

        self.nfqueues = list()

        # Track iptables rules not associated with any nfqueue object
        self.rules_added = []

        # NOTE: Constraining cache size via LRU or similar is a non-requirement
        # due to the short anticipated runtime of FakeNet-NG. If you see your
        # FakeNet-NG consuming large amounts of memory, contact your doctor to
        # find out if Ctrl+C is right for you.

        # The below callbacks are configured to be efficiently executed by the
        # handle_pkt method, incoming, and outgoing packet hooks installed by
        # the start method.

        # Network layer callbacks for nonlocal-destined packets
        #
        # Log nonlocal-destined packets and ICMP packets before they are NATted
        # to localhost
        self.nonlocal_net_cbs = [self.check_log_nonlocal, self.check_log_icmp]

        # Network and transport layer callbacks for incoming packets
        #
        # IP redirection fix-ups are only for SingleHost mode.
        self.incoming_net_cbs = []
        self.incoming_trans_cbs = [self.maybe_redir_port]
        if self.single_host_mode:
            self.incoming_trans_cbs.append(self.maybe_fixup_srcip)

        # Network and transport layer callbacks for outgoing packets.
        #
        # Must scan for nonlocal packets in the output hook and at the network
        # layer (regardless of whether supported protocols like TCP/UDP can be
        # parsed) when using the SingleHost mode of FakeNet-NG. Note that if
        # this check were performed when FakeNet-NG is operating in MultiHost
        # mode, every response packet generated by a listener and destined for
        # a remote host would erroneously be sent for potential logging as
        # nonlocal host communication. ICMP logging is performed for outgoing
        # packets in SingleHost mode because this will allow logging of the
        # original destination IP address before it was mangled to redirect the
        # packet to localhost.
        self.outgoing_net_cbs = []
        if self.single_host_mode:
            self.outgoing_net_cbs.append(self.check_log_nonlocal)
            self.outgoing_net_cbs.append(self.check_log_icmp)

        self.outgoing_trans_cbs = [self.maybe_fixup_sport]

        # IP redirection is only for SingleHost mode
        if self.single_host_mode:
            self.outgoing_trans_cbs.append(self.maybe_redir_ip)

    def startCallback(self):
        if not self.check_privileged():
            self.logger.error('The Linux Diverter requires administrative ' +
                              'privileges')
            sys.exit(1)

        ret = self.linux_capture_iptables()
        if ret != 0:
            sys.exit(1)

        if self.is_set('linuxflushiptables'):
            self.linux_flush_iptables()
        else:
            self.logger.warning('LinuxFlushIptables is disabled, this may ' +
                                'result in unanticipated behavior depending ' +
                                'upon what rules are already present')

        hookspec = namedtuple('hookspec', ['chain', 'table', 'callback'])

        callbacks = list()

        # If you are considering adding or moving hooks that mangle packets,
        # see the section of docs/internals.md titled Explaining Hook Location
        # Choices for an explanation of how to avoid breaking the Linux NAT
        # implementation.
        if not self.single_host_mode:
            callbacks.append(hookspec('PREROUTING', 'raw',
                                      self.handle_nonlocal))

        callbacks.append(hookspec('INPUT', 'mangle', self.handle_incoming))
        callbacks.append(hookspec('OUTPUT', 'raw', self.handle_outgoing))

        nhooks = len(callbacks)

        self.pdebug(DNFQUEUE, ('Discovering the next %d available NFQUEUE ' +
                    'numbers') % (nhooks))
        qnos = self.linux_get_next_nfqueue_numbers(nhooks)
        if len(qnos) != nhooks:
            self.logger.error('Could not procure a sufficient number of ' +
                              'netfilter queue numbers')
            sys.exit(1)

        fn_iface = None
        if ((not self.single_host_mode) and
                self.is_configured('linuxrestrictinterface') and not
                self.is_clear('linuxrestrictinterface')):
            self.pdebug(DMISC, 'Processing LinuxRestrictInterface config %s' %
                        self.getconfigval('linuxrestrictinterface'))
            fn_iface = self.getconfigval('linuxrestrictinterface')

        self.pdebug(DNFQUEUE, 'Next available NFQUEUE numbers: ' + str(qnos))

        self.pdebug(DNFQUEUE, 'Enumerating queue numbers and hook ' +
                    'specifications to create NFQUEUE objects')

        self.nfqueues = list()
        for qno, hk in zip(qnos, callbacks):
            self.pdebug(DNFQUEUE, ('Creating NFQUEUE object for chain %s / ' +
                        'table %s / queue # %d => %s') % (hk.chain, hk.table,
                        qno, str(hk.callback)))
            q = LinuxDiverterNfqueue(qno, hk.chain, hk.table, hk.callback,
                                     fn_iface)
            self.nfqueues.append(q)
            ok = q.start()
            if not ok:
                self.logger.error('Failed to start NFQUEUE for %s' % (str(q)))
                self.stop()
                sys.exit(1)

        if self.single_host_mode:

            if self.is_set('fixgateway'):
                if not self.linux_get_default_gw():
                    self.linux_set_default_gw()

            if self.is_set('modifylocaldns'):
                self.linux_modifylocaldns_ephemeral()

            if self.is_configured('linuxflushdnscommand'):
                cmd = self.getconfigval('linuxflushdnscommand')
                ret = subprocess.call(cmd.split())
                if ret != 0:
                    self.logger.error('Failed to flush DNS cache. Local '
                                      'machine may use cached DNS results.')

            ok, rule = self.linux_redir_icmp(fn_iface)
            if not ok:
                self.logger.error('Failed to redirect ICMP')
                self.stop()
                sys.exit(1)
            self.rules_added.append(rule)

        self.pdebug(DMISC, 'Processing interface redirection on ' +
                    'interface: %s' % (fn_iface))
        ok, rules = self.linux_iptables_redir_iface(fn_iface)

        # Irrespective of whether this failed, we want to add any
        # successful iptables rules to the list so that stop() will be able
        # to remove them using linux_remove_iptables_rules().
        self.rules_added += rules

        if not ok:
            self.logger.error('Failed to process interface redirection')
            self.stop()
            sys.exit(1)

        return True

    def stopCallback(self):
        self.pdebug(DNFQUEUE, 'Notifying NFQUEUE objects of imminent stop')
        for q in self.nfqueues:
            q.stop_nonblocking()

        self.pdebug(DIPTBLS, 'Removing iptables rules not associated with any '
                    'NFQUEUE object')
        self.linux_remove_iptables_rules(self.rules_added)

        for q in self.nfqueues:
            self.pdebug(DNFQUEUE, 'Stopping NFQUEUE for %s' % (str(q)))
            q.stop()

        if self.pcap:
            self.pdebug(DPCAP, 'Closing pcap file %s' % (self.pcap_filename))
            self.pcap.close()  # Only after all queues are stopped

        self.logger.info('Stopped Linux Diverter')

        if self.single_host_mode and self.is_set('modifylocaldns'):
            self.linux_restore_local_dns()

        if self.is_set('linuxflushiptables'):
            self.linux_restore_iptables()

        return True

    def handle_nonlocal(self, nfqpkt):
        """Handle comms sent to IP addresses that are not bound to any adapter.

        This allows analysts to observe when malware is communicating with
        hard-coded IP addresses in MultiHost mode.
        """
        try:
            pkt = LinuxPacketCtx('handle_nonlocal', nfqpkt)
            self.handle_pkt(pkt, self.nonlocal_net_cbs, [])
            if pkt.mangled:
                nfqpkt.set_payload(pkt.octets)
        # Catch-all exceptions are usually bad practice, agreed, but
        # python-netfilterqueue has a catch-all that will not print enough
        # information to troubleshoot with, so if there is going to be a
        # catch-all exception handler anyway, it might as well be mine so that
        # I can print out the stack trace before I lose access to this valuable
        # debugging information.
        except Exception:
            self.logger.error('Exception: %s' % (traceback.format_exc()))
            raise

        nfqpkt.accept()

    def handle_incoming(self, nfqpkt):
        """Incoming packet hook.

        Specific to incoming packets:
        5.) If SingleHost mode:
            a.) Conditionally fix up source IPs to support IP forwarding for
                otherwise foreign-destined packets
        4.) Conditionally mangle destination ports to implement port forwarding
            for unbound ports to point to the default listener

        No return value.
        """
        try:
            pkt = LinuxPacketCtx('handle_incoming', nfqpkt)
            self.handle_pkt(pkt, self.incoming_net_cbs,
                            self.incoming_trans_cbs)
            if pkt.mangled:
                nfqpkt.set_payload(pkt.octets)
        except Exception:
            self.logger.error('Exception: %s' % (traceback.format_exc()))
            raise

        nfqpkt.accept()

    def handle_outgoing(self, nfqpkt):
        """Outgoing packet hook.

        Specific to outgoing packets:
        4.) If SingleHost mode:
            a.) Conditionally log packets destined for foreign IP addresses
                (the corresponding check for MultiHost mode is called by
                handle_nonlocal())
            b.) Conditionally mangle destination IPs for otherwise foreign-
                destined packets to implement IP forwarding
        5.) Conditionally fix up mangled source ports to support port
            forwarding

        No return value.
        """
        try:
            pkt = LinuxPacketCtx('handle_outgoing', nfqpkt)
            self.handle_pkt(pkt, self.outgoing_net_cbs,
                            self.outgoing_trans_cbs)
            if pkt.mangled:
                nfqpkt.set_payload(pkt.octets)
        except Exception:
            self.logger.error('Exception: %s' % (traceback.format_exc()))
            raise

        nfqpkt.accept()

    def check_log_nonlocal(self, crit, pkt):
        """Conditionally log packets having a foreign destination.

        Each foreign destination will be logged only once if the Linux
        Diverter's internal log_nonlocal_only_once flag is set. Otherwise, any
        foreign destination IP address will be logged each time it is observed.
        """

        if pkt.dst_ip not in self.ip_addrs[pkt.ipver]:
            self.pdebug(DNONLOC, 'Nonlocal %s' % pkt.hdrToStr())
            first_sighting = (pkt.dst_ip not in self.nonlocal_ips_already_seen)
            if first_sighting:
                self.nonlocal_ips_already_seen.append(pkt.dst_ip)
            # Log when a new IP is observed OR if we are not restricted to
            # logging only the first occurrence of a given nonlocal IP.
            if first_sighting or (not self.log_nonlocal_only_once):
                self.logger.info(
                    'Received nonlocal IPv%d datagram destined for %s' %
                    (pkt.ipver, pkt.dst_ip))

        return None


if __name__ == '__main__':
    raise NotImplementedError
