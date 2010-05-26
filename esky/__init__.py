#  Copyright (c) 2009-2010, Cloud Matrix Pty. Ltd.
#  All rights reserved; available under the terms of the BSD License.
"""

  esky:  keep frozen apps fresh

Esky is an auto-update framework for frozen Python applications.  It provides
a simple API through which apps can find, fetch and install updates, and a
bootstrapping mechanism that keeps the app safe in the face of failed or
partial updates.

Esky is currently capable of freezing apps with py2exe, py2app, cxfreeze and
bbfreeze. Adding support for other freezer programs should be straightforward;
patches will be gratefully accepted.

The main interface is the 'Esky' class, which represents a frozen app.  An Esky
must be given the path to the top-level directory of the frozen app, and a
'VersionFinder' object that it will use to search for updates.  Typical usage
for an app automatically updating itself would look something like this:

    if hasattr(sys,"frozen"):
        app = esky.Esky(sys.executable,"http://example.com/downloads/")
        app.auto_update()

A simple default VersionFinder is provided that hits a specified URL to get
a list of available versions.  More sophisticated implementations will likely
be added in the future, and you're encouraged to develop a custom VersionFinder
subclass to meet your specific needs.

The real trick is freezing your app in a format sutiable for use with esky.
You'll almost certainly want to use the "bdist_esky" distutils command, and
should consult its docstring for full details; the following is an example
of a simple setup.py script using esky:

    from esky import bdist_esky
    from distutils.core import setup

    setup(name="appname",
          version="1.2.3",
          scripts=["appname/script1.py","appname/gui/script2.pyw"],
          options={"bdist_esky":{"includes":["mylib"]}},
         )

Invoking this setup script would create an esky for "appname" version 1.2.3:

    #>  python setup.py bdist_esky
    ...
    ...
    #>  ls dist/
    appname-1.2.3.linux-i686.zip
    #>

The contents of this zipfile can be extracted to the filesystem to give a
fully working application.  If made available online then it can also be found,
downloaded and used as an upgrade by older versions of the application.


When you find you need to move beyond the simple logic of Esky.auto_update()
(e.g. to show feedback in the GUI) then the following properties and methods
and available on the Esky class:

    app.version:                the current best available version.

    app.active_version:         the currently-executing version, or None
                                if the esky isn't for the current app.

    app.find_update():          find the best available update, or None
                                if no updates are available.

    app.fetch_version(v):       fetch the specified version into local storage.

    app.install_version(v):     install and activate the specified version.

    app.uninstall_version(v):   (try to) uninstall the specified version; will
                                fail if the version is currently in use.

    app.cleanup():              (try to) clean up various partly-installed
                                or old versions lying around the app dir.

    app.reinitialize():         re-initialize internal state after changing
                                the installed version.

If updating an application that is not writable by normal users, esky has the
ability to gain root privileges through the use of a helper program.  The
following methods control this behaviour:

    app.has_root():             check whether esky currently has root privs.

    app.get_root():             escalate to root privs by spawning helper app.

    app.drop_root():            kill helper app and drop root privileges


When properly installed, the on-disk layout of an app managed by esky looks
like this:

    prog.exe                 - esky bootstrapping executable
    updates/                 - work area for fetching/unpacking updates
    appname-X.Y.platform/    - specific version of the application
        prog.exe             - executable(s) as produced by freezer module
        library.zip          - pure-python frozen modules
        pythonXY.dll         - python DLL
        esky-bootstrap/      - files not yet moved into bootstrapping env
        esky-bootstrap.txt   - list of files expected in the bootstrapping env
        esky-lockfile.txt    - lock file to control access to in-use versions
        ...other deps...

This is also the layout of the zipfiles produced by bdist_esky.  The 
"appname-X.Y" directory is simply a frozen app directory with some extra
control information generated by esky.

To install a new version "appname-X.Z", esky performs the following steps:
    * extract it into a temporary directory under "updates"
    * move all bootstrapping files into "appname-X.Z.platm/esky-bootstrap"
    * atomically rename it into the main directory as "appname-X.Z.platform"
    * move contents of "appname-X.Z.platform/esky-bootstrap" into the main dir
    * remove the "appname-X.Z.platform/esky-bootstrap" directory

To uninstall an existing version "appname-X.Y", esky does the following
    * remove files used by only that version from the bootstrap env
    * rename its "esky-bootstrap.txt" file to "esky-bootstrap-old.txt"

Where such facilities are provided by the operating system, this process is
performed within a filesystem transaction. Nevertheless, the esky bootstrapping
executable is able to detect and recover from a failed update should such an
unfortunate situation arise.

To clean up after failed or partial updates, applications should periodically
call the "cleanup" method on their esky.  This removes uninstalled versions
and generally tries to tidy up in the main application directory.

"""

from __future__ import with_statement

__ver_major__ = 0
__ver_minor__ = 7
__ver_patch__ = 0
__ver_sub__ = ""
__version__ = "%d.%d.%d%s" % (__ver_major__,__ver_minor__,__ver_patch__,__ver_sub__)


import os
import sys
import shutil
import errno
import socket
import time
from functools import wraps

try:
    import threading
except ImportError:
    threading = None

if sys.platform != "win32":
    import fcntl
else:
    from esky.winres import is_safe_to_overwrite

from esky.errors import *
from esky.fstransact import FSTransaction
from esky.finder import DefaultVersionFinder
from esky.sudo import SudoProxy, has_root, allow_from_sudo
from esky.util import split_app_version, join_app_version,\
                      is_version_dir, is_uninstalled_version_dir,\
                      parse_version, get_best_version, appdir_from_executable,\
                      copy_ownership_info




class Esky(object):
    """Class representing an updatable frozen app.

    Instances of this class point to a directory containing a frozen app in
    the esky format.  Through such an instance the app can be updated to a
    new version in-place.  Typical use of this class might be:

        if hasattr(sys,"frozen"):
            app = esky.Esky(sys.executable,"http://example.com/downloads/")
            app.auto_update()
            app.cleanup()

    The first argument must be either the top-level application directory,
    or the path of an executable from that application.  The second argument
    is a VersionFinder object that will be used to search for updates.  If
    a string it passed, it is assumed to be a URL and is passed to a new 
    DefaultVersionFinder instance.
    """

    lock_timeout = 60*60  # 1 hour

    def __init__(self,appdir_or_exe,version_finder=None):
        if os.path.isfile(appdir_or_exe):
            self.appdir = appdir_from_executable(appdir_or_exe)
            vdir = appdir_or_exe[len(self.appdir):].split(os.sep)[1]
            details = split_app_version(vdir)
            self.name,self.active_version,self.platform = details
        else:
            self.active_version = None
            self.appdir = appdir_or_exe
        self.reinitialize()
        self._lock_count = 0
        self.version_finder = version_finder
        self.sudo_proxy = None

    def _get_version_finder(self):
        return self.__version_finder
    def _set_version_finder(self,version_finder):
        workdir = os.path.join(self.appdir,"updates")
        if version_finder is not None:
            if isinstance(version_finder,basestring):
               kwds = {"download_url":version_finder}
               version_finder = DefaultVersionFinder(**kwds)
        self.__version_finder = version_finder
    version_finder = property(_get_version_finder,_set_version_finder)

    def _get_update_dir(self):
        """Get the directory path in which self.version_finder can work."""
        return os.path.join(self.appdir,"updates")

    def get_abspath(self,relpath):
        """Get the absolute path of a file within the current version."""
        if self.active_version:
            v = join_app_version(self.name,self.active_version,self.platform)
        else:
            v = join_app_version(self.name,self.version,self.platform)
        return os.path.abspath(oss.path.join(self.appdir,v,relpath))

    def reinitialize(self):
        """Reinitialize internal state by poking around in the app directory.

        If the app directory is found to be in an inconsistent state, a
        EskyBrokenError will be raised.  This should never happen unless
        another process has been messing with the files.
        """
        best_version = get_best_version(self.appdir)
        if best_version is None:
            raise EskyBrokenError("no frozen versions found")
        details = split_app_version(best_version)
        self.name,self.version,self.platform = details

    def lock(self,num_retries=0):
        """Lock the application directory for exclusive write access.

        If the appdir is already locked by another process/thread then
        EskyLockedError is raised.  There is no way to perform a blocking
        lock on an appdir.

        Locking is achieved by creating a "locked" directory and writing the
        current process/thread ID into it.  os.mkdir is atomic on all platforms
        that we care about. 

        This also has the side-effect of failing early if the user does not
        have permission to modify the application directory.
        """
        if num_retries > 5:
            raise EskyLockedError
        if threading:
           curthread = threading.currentThread()
           try:
               threadid = curthread.ident
           except AttributeError:
               threadid = curthread.getName()
        else:
           threadid = "0"
        myid = "%s-%s-%s" % (socket.gethostname(),os.getpid(),threadid)
        lockdir = os.path.join(self.appdir,"locked")
        #  Do I already own the lock?
        if os.path.exists(os.path.join(lockdir,myid)):
            #  Update file mtime to keep it safe from breakers
            os.utime(os.path.join(lockdir,myid),None)
            self._lock_count += 1
            return True
        #  Try to make the "locked" directory.
        try:
            os.mkdir(lockdir)
        except OSError, e:
            if e.errno != errno.EEXIST:
                raise
            #  Is it stale?  If so, break it and try again.
            try:
                newest_mtime = os.path.getmtime(lockdir)
                for nm in os.listdir(lockdir):
                    mtime = os.path.getmtime(os.path.join(lockdir,nm))
                    if mtime > newest_mtime:
                        newest_mtime = mtime
                if newest_mtime + self.lock_timeout < time.time():
                    shutil.rmtree(lockdir)
                    return self.lock(num_retries+1)
                else:
                    raise EskyLockedError
            except OSError, e:
                if e.errno not in (errno.ENOENT,errno.ENOTDIR,):
                    raise
                return self.lock(num_retries+1)
        else:
            #  Success!  Record my ownership
            open(os.path.join(lockdir,myid),"wb").close()
            self._lock_count = 1
            return True
            
    def unlock(self):
        """Unlock the application directory for exclusive write access."""
        self._lock_count -= 1
        if self._lock_count == 0:
            if threading:
               curthread = threading.currentThread()
               try:
                   threadid = curthread.ident
               except AttributeError:
                   threadid = curthread.getName()
            else:
              threadid = "0"
            myid = "%s-%s-%s" % (socket.gethostname(),os.getpid(),threadid)
            lockdir = os.path.join(self.appdir,"locked")
            os.unlink(os.path.join(lockdir,myid))
            os.rmdir(lockdir)

    @allow_from_sudo()
    def has_root(self):
        """Check whether the user currently has root/administrator access."""
        return has_root()

    def get_root(self):
        """Attempt to gain root/administrator access by spawning helper app."""
        if self.has_root():
            return True
        self.sudo_proxy = SudoProxy(self)
        self.sudo_proxy.start()
        if not self.sudo_proxy.has_root():
            raise OSError(None,"could not escalate to root privileges")

    def drop_root(self):
        """Drop root privileges by killing the helper app."""
        if self.sudo_proxy is not None:
            self.sudo_proxy.close()
            self.sudo_proxy = None

    @allow_from_sudo()
    def cleanup(self):
        """Perform cleanup tasks in the app directory.

        This includes removing older versions of the app and completing any
        failed update attempts.  Such maintenance is not done automatically
        since it can take a non-negligible amount of time.
        """
        appdir = self.appdir
        self.lock()
        try:
            best_version = get_best_version(appdir)
            new_version = get_best_version(appdir,include_partial_installs=True)
            #  If there's a partial install we must complete it, since it
            #  could have left exes in the bootstrap env and we don't want
            #  to accidentally delete their dependencies.
            if best_version != new_version:
                (_,v,_) = split_app_version(new_version)
                self.install_version(v)
                best_version = new_version
            #  If there's are pending overwrites, either do them or arrange
            #  for them to be done at shutdown.
            ovrdir = os.path.join(appdir,best_version,"esky-overwrite")
            retry_overwrite_on_shutdown = False
            if os.path.exists(ovrdir):
                try:
                    self._perform_overwrites(appdir,best_version)
                except EnvironmentError:
                    retry_overwrite_on_shutdown = True
            #  Now we can safely remove all the old versions.
            #  We except the currently-executing version, and silently
            #  ignore any locked versions.
            manifest = self._version_manifest(best_version)
            manifest.add("updates")
            manifest.add("locked")
            manifest.add(best_version)
            if self.active_version:
                manifest.add(self.active_version)
            for nm in os.listdir(appdir):
                if nm not in manifest:
                    fullnm = os.path.join(appdir,nm)
                    if is_version_dir(fullnm):
                        #  It's an installed-but-obsolete version.  Properly
                        #  uninstall it so it will clean up the bootstrap env.
                        (_,v,_) = split_app_version(nm)
                        try:
                            self.uninstall_version(v)
                        except VersionLockedError:
                            pass
                        else:
                            self._try_remove(appdir,nm,manifest)
                    elif is_uninstalled_version_dir(fullnm):
                        #  It's a partially-removed version; finish removing it.
                        self._try_remove(appdir,nm,manifest)
                    elif ".old." in nm or nm.endswith(".old"):
                        #  It's a temporary backup file; remove it.
                        self._try_remove(appdir,nm,manifest)
                    else:
                        #  It's an unaccounted-for entry in the bootstrap env.
                        #  Can't prove it's safe to remove, so leave it.
                        pass
            if self.version_finder is not None:
                self.version_finder.cleanup(self)
        finally:
            self.unlock()

    def _perform_overwrites(self,appdir,version):
        """Compelte any pending file overwrites in the given version dir."""
        ovrdir = os.path.join(appdir,version,"esky-overwrite")
        for (dirnm,_,filenms) in os.walk(ovrdir,topdown=False):
            for nm in filenms:
                ovrsrc = os.path.join(dirnm,nm)
                ovrdst = os.path.join(appdir,oversrc[len(overdir):])
                with open(ovrsrc,"rb") as fIn:
                    with open(ovrdst,"ab") as fOut:
                        fOut.seek(0)
                        chunk = fIn.read(512*16)
                        while chunk:
                            fOut.write(chunk)
                            chunk = fIn.read(512*16)
                os.unlink(ovrsrc)
            os.rmdir(dirnm)

    def _try_remove(self,appdir,path,manifest=[]):
        """Try to remove the file/directory at the given path in the appdir.

        This method attempts to remove the file or directory at the given path,
        but will fail silently under a number of conditions:

            * if a file is locked or permission is denied
            * if a directory cannot be emptied of all contents
            * if the path appears on sys.path
            * if the path appears in the given manifest

        """
        fullpath = os.path.join(appdir,path)
        if fullpath in sys.path:
            return
        if path in manifest:
            return
        try:
            if os.path.isdir(fullpath):
                #  Remove paths starting with "esky-" last, since we use
                #  these to maintain state information.
                esky_paths = []
                for nm in os.listdir(fullpath):
                    if nm.startswith("esky-"):
                        esky_paths.append(nm)
                    else:
                        self._try_remove(appdir,os.path.join(path,nm),manifest)
                for nm in sorted(esky_paths):
                    self._try_remove(appdir,os.path.join(path,nm),manifest)
                os.rmdir(fullpath)
            else:
                os.unlink(fullpath)
        except EnvironmentError, e:
            if e.errno not in self._errors_to_ignore:
                raise
    _errors_to_ignore = (errno.ENOENT, errno.EPERM, errno.EACCES, errno.ENOTDIR,
                         errno.EISDIR, errno.EINVAL, errno.ENOTEMPTY,)

    def auto_update(self):
        """Automatically install the latest version of the app.

        This method automatically performs the following sequence of actions,
        escalating to root privileges if a permission error is encountered:

            * find the latest version [self.find_update()]
            * fetch the new version [self.fetch_version()]
            * install the new version [self.install_version()]
            * attempt to uninstall the old version [self.uninstall_version()]
            * reinitialize internal state [self.reinitialize()]
            * clean up the appdir [self.cleanup()]

        This method is mostly here to help you get started.  For an app of
        any serious complexity, you will probably want to build your own
        variant that e.g. operates in a background thread, prompts the user
        for confirmation, etc.
        """
        if self.version_finder is None:
            raise NoVersionFinderError
        got_root = False
        try:
            version = self.find_update()
            if version is not None:
                #  Try to install the new version.  If it fails with
                #  a permission error, escalate to root and try again.
                try:
                    self._do_auto_update(version)
                except EnvironmentError:
                    exc_type,exc_value,exc_traceback = sys.exc_info()
                    if exc_value.errno != errno.EACCES or self.has_root():
                        raise
                    try:
                        self.get_root()
                    except Exception, e:
                        raise exc_type,exc_value,exc_traceback
                    else:
                        got_root = True
                        self._do_auto_update(version)
                self.reinitialize()
            #  Try to clean up the app dir.  If it fails with a 
            #  permission error, escalate to root and try again.
            try:
                self.cleanup()
            except EnvironmentError:
                exc_type,exc_value,exc_traceback = sys.exc_info()
                if exc_value.errno != errno.EACCES or self.has_root():
                    raise
                try:
                    self.get_root()
                except Exception, e:
                    raise exc_type,exc_value,exc_traceback
                else:
                    got_root = True
                    self.cleanup()
        finally:
            #  Drop root privileges as soon as possible.
            if got_root:
                self.drop_root()

    def _do_auto_update(self,version):
        """Actual sequence of operations for auto-update.

        This is a separate method so it can easily be retried after gaining
        root privileges.
        """
        self.fetch_version(version)
        self.install_version(version)
        try:
            self.uninstall_version(self.version)
        except VersionLockedError:
            pass

    def find_update(self):
        """Check for an available update to this app.

        This method returns either None, or a string giving the version of
        the newest available update.
        """
        if self.version_finder is None:
            raise NoVersionFinderError
        best_version = None
        best_version_p = parse_version(self.version)
        for version in self.version_finder.find_versions(self):
            version_p = parse_version(version)
            if version_p > best_version_p:
                best_version_p = version_p
                best_version = version
        return best_version

    @allow_from_sudo(str)
    def fetch_version(self,version):
        """Fetch the specified updated version of the app."""
        if self.version_finder is None:
            raise NoVersionFinderError
        #  Guard against malicious input (might be called with root privs)
        target = join_app_version(self.name,version,self.platform)
        target = os.path.join(self.appdir,target)
        assert os.path.dirname(target) == self.appdir
        #  Get the new version using the VersionFinder
        loc = self.version_finder.has_version(self,version)
        if not loc:
            loc = self.version_finder.fetch_version(self,version)
        #  Adjust permissions to match the current version
        vdir = join_app_version(self.name,self.version,self.platform)
        copy_ownership_info(os.path.join(self.appdir,vdir),loc)
        return loc

    @allow_from_sudo(str)
    def install_version(self,version):
        """Install the specified version of the app.

        This fetches the specified version if necessary, then makes it
        available as a version directory inside the app directory.  It 
        does not modify any other installed versions.
        """
        #  Extract update then rename into position in main app directory
        target = join_app_version(self.name,version,self.platform)
        target = os.path.join(self.appdir,target)
        #  Guard against malicious input (might be called with root privs)
        assert os.path.dirname(target) == self.appdir
        if not os.path.exists(target):
            self.fetch_version(version)
            source = self.version_finder.has_version(self,version)
        self.lock()
        try:
            if not os.path.exists(target):
                os.rename(source,target)
            trn = FSTransaction()
            try:
                self._unpack_bootstrap_env(version,trn)
            except Exception:
                trn.abort()
                raise
            else:
                trn.commit()
        finally:
            self.unlock()

    def _unpack_bootstrap_env(self,version,trn):
        """Unpack the bootstrap env from the given target directory."""
        vdir = join_app_version(self.name,version,self.platform)
        target = os.path.join(self.appdir,vdir)
        assert os.path.dirname(target) == self.appdir
        #  Move new bootrapping environment into main app dir.
        #  Be sure to move dependencies before executables.
        bootstrap = os.path.join(target,"esky-bootstrap")
        for nm in self._version_manifest(vdir):
            bssrc = os.path.join(bootstrap,nm)
            bsdst = os.path.join(self.appdir,nm)
            if os.path.exists(bssrc):
                #  On windows we can't atomically replace files.
                #  If they differ in a "safe" way we put them aside
                #  to overwrite at a later time.
                if sys.platform == "win32" and os.path.exists(bsdst):
                    if is_safe_to_overwrite(bssrc,bsdst):
                        ovrdir = os.path.join(target,"esky-overwrite")
                        if not os.path.exists(ovrdir):
                            os.mkdir(ovrdir)
                        trn.move(bssrc,os.path.join(ovrdir,nm))
                    else:
                        trn.move(bssrc,bsdst)
                else:
                    trn.move(bssrc,bsdst)
        #  Remove the bootstrap dir; the new version is now installed
        trn.remove(bootstrap)

    @allow_from_sudo(str)
    def uninstall_version(self,version): 
        """Uninstall the specified version of the app."""
        target_name = join_app_version(self.name,version,self.platform)
        target = os.path.join(self.appdir,target_name)
        #  Guard against malicious input (might be called with root privs)
        assert os.path.dirname(target) == self.appdir
        lockfile = os.path.join(target,"esky-lockfile.txt")
        bsfile = os.path.join(target,"esky-bootstrap.txt")
        bsfile_old = os.path.join(target,"esky-bootstrap-old.txt")
        self.lock()
        try:
            if not os.path.exists(target):
                return
            #  Clean up the bootstrapping environment in a transaction.
            #  This might fail on windows if the version is locked.
            try:
                trn = FSTransaction()
                try:
                    self._cleanup_bootstrap_env(version,trn)
                except Exception:
                    trn.abort()
                    raise
                else:
                    trn.commit()
            except EnvironmentError:
                if is_locked_version_dir(target):
                    raise VersionLockedError("version in use: %s" % (version,))
                raise
            #  Disable the version by renaming its esky-bootstrap.txt file.
            #  To avoid clobbering in-use version, respect locks on this file.
            if sys.platform == "win32":
                try:
                    os.rename(bsfile,bsfile_old)
                except EnvironmentError:
                    raise VersionLockedError("version in use: %s" % (version,))
            else:
                f = open(lockfile,"r")
                try:
                    fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
                except EnvironmentError, e:
                    if not e.errno:
                        raise
                    if e.errno not in (errno.EACCES,errno.EAGAIN,):
                        raise
                    raise VersionLockedError("version in use: %s" % (version,))
                else:
                    os.rename(bsfile,bsfile_old)
                finally:
                    f.close()
        finally:
            self.unlock()

    def _cleanup_bootstrap_env(self,version,trn):
        """Cleanup the bootstrap env populated by the given version."""
        target_name = join_app_version(self.name,version,self.platform)
        #  Get set of all files that must stay in the main appdir
        to_keep = set()
        for vname in os.listdir(self.appdir):
            if vname == target_name:
                continue
            details = split_app_version(vname)
            if details[0] != self.name:
                continue
            if parse_version(details[1]) < parse_version(version):
                continue
            to_keep.update(self._version_manifest(vname))
        #  Remove files used only by the version being removed
        to_rem = self._version_manifest(target_name) - to_keep
        for nm in to_rem:
            fullnm = os.path.join(self.appdir,nm)
            if os.path.exists(fullnm):
                trn.remove(fullnm)

    def _version_manifest(self,vdir):
        """Get the bootstrap manifest for the given version directory.

        This is the set of files/directories that the given version expects
        to be in the main app directory.
        """
        mpath = os.path.join(self.appdir,vdir,"esky-bootstrap.txt")
        manifest = set()
        try:
            with open(mpath,"rt") as mf:
                for ln in mf:
                    #  Guard against malicious input, since we might try
                    #  to manipulate these files with root privs.
                    nm = os.path.normpath(ln.strip())
                    assert not os.path.isabs(nm)
                    assert not nm.startswith("..")
                    manifest.add(nm)
        except IOError:
            pass
        return manifest




def run_startup_hooks():
    import esky.sudo
    esky.sudo.run_startup_hooks()

