import subprocess
import os
import time
import StringIO
import unittest
import tempfile
import shutil
import uuid
import sys

from pynailgun import BytesInputNailgunConnection, NailgunException, \
    NailgunConnection


POSSIBLE_NAILGUN_CODES_ON_NG_STOP = [
    NailgunException.CONNECT_FAILED,
    NailgunException.CONNECTION_BROKEN,
    NailgunException.UNEXPECTED_CHUNKTYPE,
]


if os.name == 'posix':
    def transport_exists(transport_file):
        return os.path.exists(transport_file)


if os.name == 'nt':
    import ctypes
    from ctypes.wintypes import WIN32_FIND_DATAW as WIN32_FIND_DATA
    INVALID_HANDLE_VALUE = -1
    FindFirstFile = ctypes.windll.kernel32.FindFirstFileW
    FindClose = ctypes.windll.kernel32.FindClose

    # on windows os.path.exists doen't allow to check reliably that a pipe exists
    # (os.path.exists tries to open connection to a pipe)
    def transport_exists(transport_path):
        wfd = WIN32_FIND_DATA()
        handle = FindFirstFile(transport_path, ctypes.byref(wfd))
        result = handle != INVALID_HANDLE_VALUE
        FindClose(handle)
        return result


class TestNailgunConnection(unittest.TestCase):
    def setUp(self):
        self.setUpTransport()
        self.startNailgun()

    def setUpTransport(self):
        self.tmpdir = tempfile.mkdtemp()
        if os.name == 'posix':
            self.transport_file = os.path.join(self.tmpdir, 'sock')
            self.transport_address = 'local:{0}'.format(self.transport_file)
        else:
            pipe_name = u'nailgun-test-{0}'.format(uuid.uuid4().hex)
            self.transport_address = u'local:{0}'.format(pipe_name)
            self.transport_file = ur'\\.\pipe\{0}'.format(pipe_name)

    def getNailgunUberJar(self):
        return 'nailgun-server/target/nailgun-server-0.9.2-SNAPSHOT-uber.jar'

    def startNailgun(self):
        if os.name == 'posix':
            def preexec_fn():
                # Close any open file descriptors to further separate buckd from its
                # invoking context (e.g. otherwise we'd hang when running things like
                # `ssh localhost buck clean`).
                dev_null_fd = os.open("/dev/null", os.O_RDWR)
                os.dup2(dev_null_fd, 0)
                os.dup2(dev_null_fd, 2)
                os.close(dev_null_fd)
            creationflags = 0
        else:
            preexec_fn = None
            # https://msdn.microsoft.com/en-us/library/windows/desktop/ms684863.aspx#DETACHED_PROCESS
            DETACHED_PROCESS = 0x00000008
            creationflags = DETACHED_PROCESS

        stdout = None
        if os.name == 'posix':
            stdout=subprocess.PIPE
        self.ng_server_process = subprocess.Popen(
            ['java', '-Djna.nosys=true', '-jar', self.getNailgunUberJar(), self.transport_address],
            preexec_fn=preexec_fn,
            creationflags=creationflags,
            stdout=stdout,
        )

        self.assertIsNone(self.ng_server_process.poll())

        if os.name == 'posix':
            # on *nix we have to wait for server to be ready to accept connections
            the_first_line = self.ng_server_process.stdout.readline().strip()
            self.assertTrue("NGServer" in the_first_line and "started" in the_first_line, "Got a line: {0}".format(the_first_line))
        else:
            for _ in range(0, 600):
                # on windows it is OK to rely on existence of the pipe file
                if not transport_exists(self.transport_file):
                    time.sleep(0.01)
                else:
                    break

        self.assertTrue(transport_exists(self.transport_file))

    def test_nailgun_stats_and_stop(self):
        for i in range(1, 5):
            output = StringIO.StringIO()
            with NailgunConnection(
                    self.transport_address,
                    stderr=None,
                    stdin=None,
                    stdout=output) as c:
                exit_code = c.send_command('ng-stats')
                self.assertEqual(exit_code, 0)
            actual_out = output.getvalue().strip()
            expected_out = 'com.martiansoftware.nailgun.builtins.NGServerStats: {0}/1'.format(i)
            self.assertEqual(actual_out, expected_out)

        try:
            with NailgunConnection(
                    self.transport_address,
                    cwd=os.getcwd(),
                    stderr=None,
                    stdin=None,
                    stdout=None) as c:
                c.send_command('ng-stop')
        except NailgunException as e:
            self.assertIn(e.code, POSSIBLE_NAILGUN_CODES_ON_NG_STOP)

        self.ng_server_process.wait()
        self.assertEqual(self.ng_server_process.poll(), 0)

    def test_echo_latency(self):
        class Times(object):
            def __init__(self):
                self.sum = 0
                self.count = 0
                self.min = sys.maxint
                self.max = -sys.maxint - 1

            def add(self, time):
                self.sum += time
                self.count += 1
                if time < self.min:
                    self.min = time
                if time > self.max:
                    self.max = time

            def __str__(self):
                format = lambda time: '%gms' % round(time * 1000, 1)
                return '%s (min: %s, max: %s)' % (
                    format(self.sum / self.count),
                    format(self.min),
                    format(self.max))

        TEST_INPUT = 'echo test input'

        def test(connection_class, stdin, times):
            start_time = time.time()
            output = StringIO.StringIO()
            with connection_class(
                    self.transport_address,
                    cwd=os.getcwd(),
                    stderr=None,
                    stdin=stdin,
                    stdout=output) as c:
                c.send_command('ng-echo')
                times.add(time.time() - start_time)
                self.assertEqual(TEST_INPUT, output.getvalue().strip())

        nailgun_connection_times = Times()
        bytes_connection_times = Times()

        for i in range(10):
            test(
                connection_class=NailgunConnection,
                stdin=StringIO.StringIO(TEST_INPUT),
                times=nailgun_connection_times)

            test(
                connection_class=BytesInputNailgunConnection,
                stdin=TEST_INPUT,
                times=bytes_connection_times)

        print 'NailgunConnection: %s' % nailgun_connection_times
        print 'BytesInputNailgunConnection: %s' % bytes_connection_times

    def tearDown(self):
        if self.ng_server_process.poll() is None:
            # some test has failed, ng-server was not stopped. killing it
            self.ng_server_process.kill()
        shutil.rmtree(self.tmpdir)


if __name__ == '__main__':
    for i in range(50):
        was_successful = unittest.main(exit=False).result.wasSuccessful()
        if not was_successful:
            sys.exit(1)
