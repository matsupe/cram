"""
This module defines the CramFile class, which is used to store many of
job invocations to be run later in the same MPI job.  A job invocation
is simply the context needed to run a single MPI job: number of
processes, working directory, command-line arguments, and environment.

The format is designed to be read easily from a C program, so it only
writes simple ints and strings.  Ints are all unsigned. Strings start
with an integer length, after which all the characters are written out.

CramFiles use a very simple form of compression to store each job's
environment, since the environment can grow to be very large and is
usually quite redundant.  For each job appended to a CramFile after
the first, we compare its environment to the first job's environment,
and we only store the differences.

We could potentially get more compression out of comparing each
environment to its successor, but that would mean that you'd need to
read all preceding jobs to decode one.  We wanted a format that would
allow scattering jobs very quickly to many MPI processes.

Sample usage:

  cf = CramFile('file.cram', 'w')
  cf.append(Job(64,             # number of processes
                os.getcwd(),    # working dir, as a string
                sys.argv[1:],   # cmdline args, as a list
                os.env))        # environment, as a dict.
  cf.close()

To read from a CramFile, use len and iterate:

  cf = CramFile('file.cram')
  num_jobs = len(cf)
  for job in cf:
      # do something with job
  cf.close()

Or just index it:

  cf = CramFile('file.cram')
  job4 = cf[4]
  cf.close()

Here is the CramFile format.  '*' below means that the section can be
repeated a variable number of times.

Type       Name
========================================================================
Header
------------------------------------------------------------------------
int(4)       0x6372616d ('cram')
int(4)       Version
int(4)       # of jobs
int(4)       # of processes

* Job records
------------------------------------------------------------------------
  int(4)     Number of processes
  str        Working dir

  int(4)     Number of command line arguments
   * str       Command line arguments, in original order

  int(4)     Number of subtracted env var names (0 for first record)
   * str       Subtracted env vars in sorted order.
  int(4)     Number of added or changed env vars
   *  str      Names of added/changed var
   *  str      Corresponding value

Env vars are stored alternating keys and values, in sorted order by key.
========================================================================
"""
import os

from collections import defaultdict
from contextlib import contextmanager, closing

from cram.serialization import *
import llnl.util.tty as tty

# Magic number goes at beginning of file.
_magic = 0x6372616d

# Increment this when the binary format changes (hopefully infrequent)
_version = 1

# Offsets of file header fields
_magic_offset   = 0
_version_offset = 4
_njobs_offset   = 8
_nprocs_offset  = 12


@contextmanager
def save_position(stream):
    """Context for doing something while saving the current position in a
       file."""
    pos = stream.tell()
    yield
    stream.seek(pos)


def compress(base, modified):
    """Compare two dicts, modified and base, and return a diff consisting
       of two parts:

       1. missing: set of keys in base but not in modified
       2. changed: dict of key:value pairs either in modified but not in base,
          or that have different values in base and modified.

       This simple compression scheme is used to compress the environment
       in cramfiles, since that takes up the bulk of the space.
    """
    missing = set(base.keys()).difference(modified)
    changed = { k:v for k,v in modified.items()
                if k not in base or base[k] != v }
    return missing, changed


def decompress(base, missing, changed):
    """Given the base dict and the output of compress(), reconstruct the
       modified dict."""
    d = base.copy()
    for k in missing:
        del d[k]
    d.update(changed)
    return d


class Job(object):
    """Simple class to represent one job invocation packed into a cramfile.
       This contains all environmental context needed to launch the job
       later from within MPI.
    """
    def __init__(self, num_procs, working_dir, args, env):
        self.num_procs = num_procs
        self.working_dir = working_dir
        self.args = args
        self.env = env

    def __eq__(self, other):
        return (self.num_procs == other.num_procs and
                self.working_dir == other.working_dir and
                self.args == other.args and
                self.env == other.env)

    def __ne__(self, other):
        return not (self == other)


class CramFile(object):
    """A CramFile compactly stores a number of Jobs, so that they can
       later be run within the same MPI job by cram.
    """
    def __init__(self, filename, mode='r'):
        """The CramFile constructor functions much like open().

           The constructor takes a filename and an I/O mode, which can
           be 'r', 'w', or 'a', for read, write, or append.

           Opening a CramFile for writing will create a file with a
           simple header containing no jobs.
        """
        # Jobs read in from the file.
        self.jobs = []

        self.mode = mode
        if mode not in ('r', 'w', 'a'):
            raise ValueError("Mode must be 'r', 'w', or 'a'.")

        if mode == 'r':
            if not os.path.exists(filename) or os.path.isdir(filename):
                tty.die("No such file: %s" % filename)

            self.stream = open(filename, 'rb')
            self._read_header()

        elif mode == 'w' or (mode == 'a' and not os.path.exists(filename)):
            self.stream = open(filename, 'wb')
            self.version = _version
            self.num_jobs = 0
            self.num_procs = 0
            self._write_header()

        elif mode == 'a':
            self.stream = open(filename, 'rb+')
            self._read_header()
            self.stream.seek(0, os.SEEK_END)


    def _read_header(self):
        """Jump to the beginning of the file and read the header.  The cursor
           will be at the end of the header on completion, so you will need to
           save it if you want to end up somewhere else."""
        self.stream.seek(0)

        magic = read_int(self.stream, 4)
        if magic != _magic:
            raise IOError("%s is not a Cramfile!")

        self.version = read_int(self.stream, 4)
        if self.version != _version:
            raise IOError(
                "Version mismatch: File has version %s, but this is version %s"
                % (self.version, _version))

        self.num_jobs = read_int(self.stream, 4)
        self.num_procs = read_int(self.stream, 4)

        # read in the first job automatically if it is there, since
        # it is used for compression of subsequent jobs.
        if self.num_jobs > 0:
            self._read_job()


    def _write_header(self):
        """Jump to the beginning of the file and write the header."""
        self.stream.seek(0)
        write_int(self.stream, _magic, 4)
        write_int(self.stream, self.version, 4)
        write_int(self.stream, self.num_jobs, 4)
        write_int(self.stream, self.num_procs, 4)


    def append(self, job):
        """Appends a job to a cram file, compressing the environment in the
           process."""
        if self.mode == 'r':
            raise IOError("Cannot append to CramFile opened for reading.")

        # Number of processes
        write_int(self.stream, job.num_procs, 4)

        # Working directory
        write_string(self.stream, job.working_dir)

        # Command line arguments
        write_int(self.stream, len(job.args), 4)
        for arg in job.args:
            write_string(self.stream, arg)

        # Compress using first dict
        missing, changed = compress(
            self.jobs[0].env if self.jobs else {}, job.env)

        # Subtracted env var names
        write_int(self.stream, len(missing), 4)
        for key in sorted(missing):
            write_string(self.stream, key)

        # Changed environment variables
        write_int(self.stream, len(changed), 4)
        for key in sorted(changed.keys()):
            write_string(self.stream, key)
            write_string(self.stream, changed[key])

        with save_position(self.stream):
            # Update total number of jobs in file.
            self.num_jobs += 1
            self.stream.seek(_njobs_offset)
            write_int(self.stream, self.num_jobs, 4)

            # Update total number of processes in all jobs.
            self.num_procs += job.num_procs
            self.stream.seek(_nprocs_offset)
            write_int(self.stream, self.num_procs, 4)

        self.jobs.append(job)


    def _read_job(self):
        """Read the next unread job out of the CramFile and append it to
           self.jobs.

           This is an internal method because it's used to load stuff
           that isn't already in memory.  Client code should use
           len(), [], or iterate to read jobs from CramFiles.
        """
        # Number of processes
        num_procs   = read_int(self.stream, 4)

        # Working directory
        working_dir = read_string(self.stream)

        # Command line arguments
        num_args    = read_int(self.stream, 4)
        args        = []
        for i in xrange(num_args):
            args.append(read_string(self.stream))

        # Subtracted environment variables
        num_missing = read_int(self.stream, 4)
        missing     = []
        for i in xrange(num_missing):
            missing.append(read_string(self.stream))

        # Changed environment variables
        num_changed = read_int(self.stream, 4)
        changed = {}
        for i in xrange(num_changed):
            key = read_string(self.stream)
            val = read_string(self.stream)
            changed[key] = val

        # Decompress using first dictionary
        env = decompress(self.jobs[0].env if self.jobs else {},
                         missing, changed)

        self.jobs.append(Job(num_procs, working_dir, args, env))
        return self.jobs[-1]


    def __getitem__(self, index):
        """Return the index-th job stored in this CramFile."""
        if self.mode == 'r':
            while (len(self.jobs) < index+1 and
                   len(self.jobs) < self.num_jobs):
                self._read_job()

        return self.jobs[index]


    def __iter__(self):
        """Iterate over all jobs in the CramFile."""
        for job in self.jobs:
            yield job

        while len(self.jobs) < self.num_jobs:
            yield self._read_job()


    def __len__(self):
        """Number of jobs in the CramFile."""
        return self.num_jobs


    def close(self):
        """Close the underlying file stream."""
        self.stream.close()
