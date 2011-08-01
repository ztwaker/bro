import atexit
import bz2
import csv
import gzip
import hashlib
import os
import os.path
import re
import shutil
import subprocess
import tempfile

class LogOptions(object):
    verbose = False

class BroLogEntry(object):
    def __init__(self, typespec, vals):
        self._typespec = typespec
        val_index = 0
        self._fields = []
        for field, typename in typespec.types():
           self._fields.append(field)
           setattr(self, field, typespec.get_val(vals[val_index], typename))
           val_index += 1

    def __str__(self):
        ret = "( "
        for f in self._fields:
            ret += "(" + f + ", " + str(getattr(self, f)) + ") "
        return (ret + ")")

class BroLogGenerator(object):
    def __init__(self, log_list):
        self._logs = log_list

    def entries(self):
        if not self._logs:
            return
        
        for log in self._logs:
            log_type = log.type()
            log_fd = log_type.open(log.path())
            if not log_fd:
                continue
            for entry in log_fd:
                yield BroLogEntry(log_type, entry)

class BroLogFile(object):
    def __init__(self, path):
        self._path = path
        self._typespec = BroLogManager.get_typespec(path)()
        self._valid = self._typespec.load(path)
        self._fd = None

    def type(self):
        return self._typespec

    def type_id(self):
        return self._typespec.id()

    def open(self):
        if(self._valid):
            self._fd = self._typespec.open(path)
            return self._fd
        return None

    def valid(self):
        return self._valid

    def path(self):
        return self._path

    def bro_path(self):
        return self._typespec.get_bro_path()

class BroLogManager(object):
    logtypes = dict()
    EXT_EXPR = re.compile(r"[^/].*?\.(.*)$")

    @staticmethod
    def supports(path):
        base, fname = os.path.split(path)
        return BroLogManager.get_ext(fname) in BroLogManager.logtypes

    @staticmethod
    def get_typespec(path):
        base, fname = os.path.split(path)
        return BroLogManager.logtypes[ BroLogManager.get_ext(fname) ]

    @staticmethod
    def get_ext(path):
        m = BroLogManager.EXT_EXPR.search(path)
        if(m):
            return m.group(1)
        return None

    @staticmethod
    def register_type(file_ext, target):
        BroLogManager.logtypes[file_ext] = target

    def __init__(self):
        self._path = None
        self._logfiles = []
        self._logobj = []
        self._total_count = 0
        self._success_count = 0

    def load(self, paths):
        map(self.open, paths)

    def open(self, path):
        self._path = path
        if(os.path.isdir(path)):
            os.path.walk(path, lambda arg, dirname, fnames: arg.extend( [ os.path.join(os.path.abspath(dirname), f) for f in fnames ] ), self._logfiles)
        else:
            self._logfiles.append(path)
        self._logfiles = list(set(self._logfiles))  # Remove duplicates
        self._logfiles = [f for f in self._logfiles if BroLogManager.supports(f) ]  # Only keep supported file types
        self._total_count = len(self._logfiles)
        self._logobj = [ BroLogFile(f) for f in self._logfiles ]
        self._logobj = [ f for f in self._logobj if f.valid() ]
        self._success_count = len(self._logobj)
        # self._types = [ obj._typespec for obj in self._logobj ]
        # self._types = set(self._types)
        # self._type_count = len(self._types)
        self._logs = dict()
        for obj in self._logobj:
            if obj.bro_path() not in self._logs:
                self._logs[obj.bro_path()] = []
            self._logs[obj.bro_path()].append(obj)
        self._type_count = len(self._logs)
        self._log_gen = dict()
        for key in self._logs.keys():
            self._log_gen[key] = BroLogGenerator(self._logs[key])
        # Quick sanity check; make sure types are consistent across bro log paths.  Note that if
        # this is not true, Bad Things (tm) could happen.
        for key in self._logs.keys():
            tmp_id = None
            for obj in self._logs[key]:
                if not tmp_id:
                    tmp_id = obj.type_id()
                else:
                    if(tmp_id != obj.type_id()):
                        print "[WARNING] Multiple types found for path: " + obj.bro_path()
                        # print tmp_id
        del self._logobj

    def __getitem__(self, key):
        if key in self._log_gen:
            return self._log_gen[key].entries()
        return None

    def print_stats(self):
        print "Found " + str(self._total_count) + " logfiles."
        print "Successfully loaded " + str(self._success_count) + " logfiles."
        print "Identified " + str(self._type_count) + " unique bro paths."

class BaseLogSpec(object):
    def __init__(self):
        self._types = []
        self._bro_log_path = ""
        self._valid = False

    def get_bro_path(self):
        return self._bro_log_path

    def id(self):
        m = hashlib.md5()
        m.update(str(self._bro_log_path))
        m.update(str(self._types))
        return m.hexdigest()

    def valid(self):
        return self._valid

    def __str__(self):
        return (self.id() + " : " + self._bro_log_path + " -- " + str(self._types))

    def __repr__(self):
        return (self.id() + " : " + self._bro_log_path + " -- " + str(self._types))

    def __eq__(self, other):
        if issubclass(other.__class__, BaseLogSpec):
            return self.id() == other.id()
        return False

    def __ne__(self, other):
        return not self.__eq__(other)

    def __hash__(self):
        return hash(self.id())

class DsLogSpec(BaseLogSpec):
    RE_TYPESPEC = re.compile(r"<!--(.*?)=(.*?)-->")
    RE_PATHSPEC = re.compile(r'<ExtentType name="(.*?)" version="1.0" namespace="bro-ids.org">')  # e.g. <ExtentType name="mime" version="1.0" namespace="bro-ids.org">
    TIME_SCALE = 100000.0
    DS_EXTRACT_DIR = tempfile.mkdtemp()

    @staticmethod
    def cleanup():
        print "Cleaning up " + DsLogSpec.DS_EXTRACT_DIR
        shutil.rmtree(DsLogSpec.DS_EXTRACT_DIR)

    def __init__(self):
        self._types = []
        self._bro_log_path = ""
        self._opened = None
        self._tpath = None
        self._valid = False

    def get_val(self, val, type_info):
        if(type_info == 'double'): 
            return float(val)
        if(type_info == 'time' or type_info == 'interval'):
            return float(val) / DsLogSpec.TIME_SCALE
        if(type_info == 'int' or type_info == 'counter' or type_info == 'port' or type_info == 'count'):
            return int(val)
        return val

    def id(self):
        m = hashlib.md5()
        m.update(str(self._bro_log_path))
        m.update(str(self._types))
        return m.hexdigest()

    def open(self, path):
        if not (self._opened == path and self._tpath):
            if(LogOptions.verbose):
                print "Extracting " + path
            self._opened = path
            tfd, self._tpath = tempfile.mkstemp()
            os.close(tfd)
            os.system('ds2txt --csv --skip-extent-fieldnames --separator="\t" ' + path + ' > ' + self._tpath)
        return csv.reader( open(self._tpath, "rb"), delimiter='\t' )

    def close(self, fd):
        fd.close()

    def load(self, path):
        xml_str = ""
        # This will pull ALL schema definitions out of the DataSeries log file.  Our comment hack works because our schema *should* be the only thing defined
        # within DataSeries that has comment tags of the form defined above; if this assumption does not hold, the typespec will be incorrect.
        tload = subprocess.Popen(['ds2txt', '--skip-index', '--select', "DataSeries: XmlType", "*", str(path)], shell=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        res = tload.communicate()
        if(len(res[0]) > 0):
            xml_str = reduce(lambda x, y: x + y, res[0])
        else:
            # print "Could not load: " + path
            self._valid = False
            return False
        if(self.parse(xml_str)):
            self._fd = self.open(path)
            self._valid = True
            return True
        self._valid = False
        return False

    def parse(self, parse_string):
        for line in parse_string.splitlines():
            m = DsLogSpec.RE_TYPESPEC.match(line)
            if m:
                self._types.append( (m.group(1), m.group(2)) )
            m = DsLogSpec.RE_PATHSPEC.match(line)
            if m:
                self._bro_log_path = m.group(1)
        if(len(self._bro_log_path) == 0):
            # print "no bro path assignment (e.g. the 'conn' bit of something like 'conn.log' or 'conn.ds') found.  Skipping file..."
            return False
        if(len(self._types) == 0):
            return False
        return True
    
    def types(self):
        return self._types

class AsciiLogSpec(BaseLogSpec):
    RE_TYPESPEC = re.compile(r"\s*#(.*?)\n?")  # Pull out everything after a comment character
    RE_PATHSPEC = re.compile(r"\s*#\s*path:'(.*)'")  # Pull out the logfile path name (as defined by bro; this is *NOT* the filesystem path)
    RE_SEPARATOR = re.compile(r"\s*#\s*separator:'(.*)'")  # Pull out the separator character
    RE_TYPE_ENTRY = re.compile(r"(.*)=(.*)")  # Extract FIELD=BRO_TYPE

    def __init__(self):
        self._types = []
        self._bro_log_path = ""
        self._separator = ""
        self._valid = False

    def get_val(self, val, type_info):
        if(val == '-'):
            return None
        if(type_info == 'double' or type_info == 'time' or type_info == 'interval'):
            return float(val)
        if(type_info == 'int' or type_info == 'counter' or type_info == 'port' or type_info == 'count'):
            return int(val)
        return val

    def raw_open(self, path):
        if(BroLogManager.get_ext(path) == 'log.gz'):
            ascii_file = gzip.GzipFile(path)
        elif(BroLogManager.get_ext(path) == 'log.bz2'):
            ascii_file = bz2.BZ2File(path)
        else:
            ascii_file = open(path)
        return ascii_file
  
    def open_gen(self, fd):
        for entry in fd:
            tentry = entry[0].strip()
            if(tentry[0] == '#'):
                continue
            yield entry

    def open(self, path):
        if(BroLogManager.get_ext(path) == 'log.gz'):
            ascii_file = gzip.GzipFile(path)
        elif(BroLogManager.get_ext(path) == 'log.bz2'):
            ascii_file = bz2.BZ2File(path)
        else:
            ascii_file = open(path, 'rb')
        return self.open_gen(csv.reader(ascii_file, delimiter=self._separator))

    def close(self, fd):
        fd.close()

    def load(self, path):
        ascii_file = self.raw_open(path)
        if(ascii_file):
            key = ascii_file.readline()
            m = AsciiLogSpec.RE_PATHSPEC.match(ascii_file.readline())
            if not m:
                # print "no bro path assignment (e.g. the 'conn' bit of something like 'conn.log' or 'conn.ds') found.  Skipping file..."
                return False
            self._bro_log_path = m.group(1)
            m = AsciiLogSpec.RE_SEPARATOR.match(ascii_file.readline())
            if not m:
                # print "no separator found.  Skipping file..."
                return False
            self._separator = m.group(1)
            fields = ascii_file.readline()
            if not self.parse(fields):
                # print "Unsupported logfile: " + path
                return False
            return True
        self.close(ascii_file)

    def parse(self, type_info):
        m = AsciiLogSpec.RE_TYPESPEC.match(type_info)
        if not m:
            return False
        type_array = re.sub("\s*#\s*", '', type_info).split(" ")
        m = [AsciiLogSpec.RE_TYPE_ENTRY.match(entry) for entry in type_array]
        self._types = [ ( entry.group(1), entry.group(2) ) for entry in m]
        if(len(self._types) == 0):
            return False
        #for e in self._types:
        #    print e
        return True

    def types(self):
        return self._types

BroLogManager.register_type('log', AsciiLogSpec)
BroLogManager.register_type('log.gz', AsciiLogSpec)
BroLogManager.register_type('log.bz2', AsciiLogSpec)
BroLogManager.register_type('ds', DsLogSpec)

atexit.register(DsLogSpec.cleanup)
