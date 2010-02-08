#  Copyright (c) 2009, Cloud Matrix Pty. Ltd.
#  All rights reserved; available under the terms of the BSD License.
"""

  esky.bdist_esky.f_py2exe:  bdist_esky support for py2exe

"""


import os
import re
import sys
import imp
import time
import zipfile
import marshal
import struct
import shutil
import inspect
import zipfile
from glob import glob


from py2exe.build_exe import py2exe

import esky
from esky.util import is_core_dependency
from esky import winres


def freeze(dist):
    """Freeze the given distribution data using bbfreeze."""
    includes = dist.includes
    excludes = dist.excludes
    options = dist.freezer_options
    #  Merge in any encludes/excludes given in freezer_options
    includes.append("esky")
    for inc in options.pop("includes",()):
        includes.append(inc)
    for exc in options.pop("excludes",()):
        excludes.append(exc)
    #  py2exe expects some arguments on the main distribution object.
    #  We handl data_files ourselves, so fake it out for py2exe.
    dist.distribution.console = []
    dist.distribution.windows = []
    my_data_files = dist.distribution.data_files
    dist.distribution.data_files = []
    for script in dist.get_scripts():
        if script.endswith(".pyw"):
            dist.distribution.windows.append(script)
        else:
            dist.distribution.console.append(script)
    if "zipfile" in options:
        dist.distribution.zipfile = options.pop("zipfile")
    #  Create the py2exe cmd and adjust its options
    cmd = py2exe(dist.distribution)
    cmd.includes = includes
    cmd.excludes = excludes
    if "bundle_files" not in options and "zipfile" not in options:
        #  If the user hasn't expressed a preference, bundle all PYD libs
        #  into the zipfile as well.  This allows us to use a more efficient
        #  chainloader mechanism.
        cmd.bundle_files = 2
    for (nm,val) in options.iteritems():
        setattr(cmd,nm,val)
    cmd.dist_dir = dist.freeze_dir
    cmd.finalize_options()
    #  Actually run the freeze process
    cmd.run()
    #  Copy data files into the freeze dir
    dist.distribution.data_files = my_data_files
    for (src,dst) in dist.get_data_files():
        dst = os.path.join(dist.freeze_dir,dst)
        dstdir = os.path.dirname(dst)
        if not os.path.isdir(dstdir):
            dist.mkpath(dstdir)
        dist.copy_file(src,dst)
    #  Copy package data into the library.zip
    #  For now, we don't try to put package data into a bundled zipfile.
    if dist.distribution.zipfile is not None:
        lib = zipfile.ZipFile(os.path.join(dist.freeze_dir,"library.zip"),"a")
        for (src,arcnm) in dist.get_package_data():
            lib.write(src,arcnm)
        lib.close()
    else:
        for (src,arcnm) in dist.get_package_data():
            err = "zipfile=None can't be used with package_data (yet...)"
            raise RuntimeError(err)
    #  There's no need to copy library.zip into the bootstrap env, as the
    #  chainloader will run before py2exe goes looking for it.
    pass
    #  Create the bootstraping code, using custom code if specified.
    #  It gets stored as a marshalled list of code objects directly in the exe.
    code_source = [inspect.getsource(esky.bootstrap)]
    code_source.append(_FAKE_ESKY_BOOTSTRAP_MODULE)
    code_source.append(_CUSTOM_WIN32_CHAINLOADER)
    if dist.bootstrap_module is None:
        code_source.append("bootstrap()")
    else:
        bsmodule = __import__(dist.bootstrap_module)
        for submod in dist.boostrap_module.split(".")[1:]:
            bsmodule = getattr(bsmodule,submod)
        code_source.append(inspect.getsource(bsmodule))
    code_source = "\n".join(code_source)
    code = marshal.dumps([compile(code_source,"__main__.py","exec")])
    coderes = struct.pack("iiii",
                     0x78563412, # a magic value used for integrity checking,
                     0, # no optimization
                     False,  # normal buffered output
                     len(code),
                     ) + "\000" + code + "\000"
    #  We bundle the python DLL into all bootstrap executables, even if it's
    #  not bundled in the frozen distribution.  This helps keep the boostrap
    #  env small and minimises the chances of something going wrong.
    pydll = u"python%d%d.dll" % sys.version_info[:2]
    frozen_pydll = os.path.join(dist.freeze_dir,pydll)
    if os.path.exists(frozen_pydll):
        with open(frozen_pydll,"rb") as f:
            pydll_bytes = f.read()
    else:
        pydll_bytes = None
    #  Copy the loader program for each script into the bootstrap env.
    for script in dist.get_scripts():
        nm = os.path.basename(script)
        if nm.endswith(".py") or nm.endswith(".pyw"):
            nm = ".".join(nm.split(".")[:-1]) + ".exe"
        exepath = dist.copy_to_bootstrap_env(nm)
        #  Insert the bootstrap code into the exe as a resource.
        #  This appears to have the happy side-effect of stripping any extra
        #  data from the end of the exe, which is exactly what we want when
        #  zipfile=None is specified; otherwise each bootstrap EXE would also
        #  contain the whole bundled zipfile.
        winres.add_resource(exepath,coderes,u"PYTHONSCRIPT",1,0)
        #  Inline the pythonXY.dll as a resource in the exe.
        if pydll_bytes is not None:
            winres.add_resource(exepath,pydll_bytes,pydll.upper(),1,0)
    #  Copy any core dependencies into the bootstrap env.
    for nm in os.listdir(dist.freeze_dir):
        if is_core_dependency(nm) and nm != pydll:
            dist.copy_to_bootstrap_env(nm)


#  Code to fake out any bootstrappers that try to import from esky.
_FAKE_ESKY_BOOTSTRAP_MODULE = """
class __fake:
  __all__ = ()
sys.modules["esky"] = __fake()
sys.modules["esky.bootstrap"] = __fake()
"""


#  On Windows, execv is flaky and expensive.  If the chainloader is the same
#  python version as the target exe, we can munge sys.path to bootstrap it
#  into the existing process.
#
#  We need to read the script to execute as a resource from the exe, so this
#  only works if we can bootstrap a working ctypes module.  We then insert
#  the source code from esky.winres directly into this function.
_CUSTOM_WIN32_CHAINLOADER = """
_orig_chainload = chainload
def chainload(target_dir):
  # winres imports sys, so do it here to avoid issues with local varialbes.
  import sys
  # careful to escape percent-sign, this gets interpolated below
  pydll = "python%%s%%s.dll" %% sys.version_info[:2]
  mydir = dirname(sys.executable)
  if not exists(pathjoin(target_dir,pydll)):
      _orig_chainload(target_dir)
  else:
      for nm in listdir(target_dir):
          if nm == pydll:
              continue
          if nm.lower().endswith(".pyd") or nm.lower().endswith(".dll"):
              #  The freeze dir contains unbundled C extensions.  We can't
              #  chainload them since they're linked against a physical pydll
              _orig_chainload(target_dir)
      sys.bootstrap_executable = sys.executable
      sys.executable = pathjoin(target_dir,basename(sys.executable))
      sys.argv[0] = sys.executable
      for i in xrange(len(sys.path)):
          sys.path[i] = sys.path[i].replace(mydir,target_dir)
      libfile = pathjoin(target_dir,"library.zip")
      if exists(libfile) and libfile not in sys.path:
          sys.path.append(libfile)
      try:
          import zipextimporter; zipextimporter.install()
      except ImportError:
          pass
      try:
          import ctypes
          import struct
          import marshal
      except ImportError:
          _orig_chainload(target_dir)
      # the source for esky.winres gets inserted below:
      %s
      # now we magically have the load_resource function :-)
      try:
          data = load_resource(sys.executable,u"PYTHONSCRIPT",1,0)
      except EnvironmentError:
          _orig_chainload(target_dir)
      else:
          del sys.modules["esky"]
          del sys.modules["esky.bootstrap"]
          headsz = struct.calcsize("iiii")
          (magic,optmz,unbfrd,codesz) = struct.unpack("iiii",data[:headsz])
          assert magic == 0x78563412
          # TODO: what do I need to do for "optimized" and "unbuffered"?
          # do these matter for run-of-the-mill exes?
          codestart = headsz
          # skip over the archive name
          while data[codestart] != "\\0":
              codestart += 1
          codestart += 1
          codelist = marshal.loads(data[codestart:codestart+codesz])
          for code in codelist:
              exec code in {}, locals()
""" % (inspect.getsource(winres).replace("\n","\n"+" "*6),)

