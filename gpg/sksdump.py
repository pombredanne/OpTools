#!/usr/bin/env python3

# NOTE: This was written for systemd systems only.
# Tweaking would be needed for non-systemd systems
# (since every non-systemd uses their own init system callables...)
#
# Thanks to Matt Rude and https://gist.github.com/mattrude/b0ac735d07b0031bb002 so I can know what the hell I'm doing.
#
# IMPORTANT: This script uses certaion permissions functions that require some forethought. You can either run as root,
# which is the "easy" way, OR you can run as the sks user. Has to be one or the other; you'll SERIOUSLY mess things up
# otherwise. If you run as the sks user, MAKE SURE the following is set in your sudoers (where SKSUSER is the username sks runs as:
#           Cmnd_Alias SKSCMDS = /usr/bin/systemctl start sks-db,\
#                                /usr/bin/systemctl stop sks-db,\
#                                /usr/bin/systemctl start sks-recon,\
#                                /usr/bin/systemctl stop sks-recon
#           SKSUSER ALL = NOPASSWD: SKSCMDS


import argparse
import datetime
import getpass
import os
import subprocess
from pwd import getpwnam
from grp import getgrnam

NOW = datetime.datetime.utcnow()
NOWstr = NOW.strftime('%Y-%m-%d')

# TODO:
# - use a config file system
# - cleanup/rotation should be optional

sks = {
    # chowning - MAKE SURE THIS IS THE USER SKS RUNS AS.
    'user': 'sks',
    # chowning
    'group': 'sks',
    # Where your SKS DB is
    'basedir': '/var/lib/sks',
    # Where the dumps should go. This dir is scrubbed based on mtime, so ONLY use this dir for dumps.
    'destdir': '/srv/http/sks/dumps',
    # If None, don't compress dumps. If one of: 'xz', 'gz', 'bz2', or 'lrz' (for lrzip) then use that compression algo.
    'compress': 'lrz',
    # The service name(s) to stop for the dump and to start again afterwards.
    'svcs': ['sks-db', 'sks-recon'],
    # I would hope this is self-explanatory. If not, this is where we log the outout of the sks dump process. (and any rsync errors, too)
    'logfile': '/var/log/sksdump.log',
    # If not None value, where we should push the dumps when done. Can be a local path too, obviously.
    'rsync': 'root@mirror.square-r00t.net:/srv/http/sks/dumps/.',
    # How many previous days of dumps should we keep?
    'days': 1,
    # How many keys to include per dump file
    'dumpkeys': 15000
}


## Make things a little more sensical/catch some common errors
# symlinks? relative path? HOME reference? WE HANDLE IT ALL.
sks['destdir'] = os.path.realpath(os.path.abspath(os.path.expanduser(sks['destdir'])))
if sks['compress'] == 'lrzip':
    sks['compress'] == 'lrz'

def svcMgmt(op):
    if op not in ('start', 'stop'):
        raise ValueError('Operation must be start or stop')
    for svc in sks['svcs']:
        cmd = ['/usr/bin/systemctl', op, svc]
        if getpass.getuser() != 'root':
            cmd.insert(0, 'sudo')
        subprocess.run(cmd)
    return()

def destPrep():
    nowdir = os.path.join(sks['destdir'], NOWstr)
    curdir = os.path.join(sks['destdir'], 'current')
    PAST = NOW - datetime.timedelta(days = sks['days'])
    for thisdir, dirs, files in os.walk(sks['destdir'], topdown = False):
        for f in files:
            try:  # we use a try here because if the link's broken, the script bails out.
                fstat = os.stat(os.path.join(thisdir, f))
                mtime = fstat.st_mtime
                if int(mtime) < PAST.timestamp():
                    os.remove(os.path.join(thisdir, f))
            except FileNotFoundError:  # broken symlink
                try:
                    os.remove(os.path.join(thisdir, f))
                except:
                    pass  # just... ignore it. it's fine, whatever.
            # Delete if empty dir
            if len(os.listdir(thisdir)) == 0:
                os.rmdir(thisdir)
        for d in dirs:
            if len(os.listdir(os.path.join(thisdir, d))) == 0:
                os.rmdir(os.path.join(thisdir, d))
    #try:
    #    os.removedirs(sks['destdir'])  # Remove empty dirs
    #except:
    #    pass  # thisisfine.jpg
    os.makedirs(nowdir, exist_ok = True)
    if getpass.getuser() == 'root':
        uid = getpwnam(sks['user']).pw_uid
        gid = getgrnam(sks['group']).gr_gid
        for d in (sks['destdir'], nowdir):  # we COULD set it as part of the os.makedirs, but iirc it doesn't set it for existing dirs
            os.chown(d, uid, gid)
    if os.path.isdir(curdir):
        os.remove(curdir)
    os.symlink(NOWstr, curdir, target_is_directory = True)
    return()

def dumpDB():
    destPrep()
    os.chdir(sks['basedir'])
    svcMgmt('stop')
    cmd = ['sks',
           'dump',
           str(sks['dumpkeys']),  # How many keys per dump?
           os.path.join(sks['destdir'], NOWstr),  # Where should it go?
           'keydump.{0}'.format(NOWstr)]  # What the filename prefix should be
    if getpass.getuser() == 'root':
        cmd2 = ['sudo', '-u', sks['user']]
        cmd2.extend(cmd)
        cmd = cmd2
    with open(sks['logfile'], 'a') as f:
        f.write('===== {0} =====\n'.format(str(datetime.datetime.utcnow())))
        subprocess.run(cmd, stdout = f, stderr = f)
    svcMgmt('start')
    return()

def compressDB():
    if not sks['compress']:
        return()
    curdir = os.path.join(sks['destdir'], NOWstr)
    for thisdir, dirs, files in os.walk(curdir):  # I use os.walk here because we might handle this differently in the future...
        files.sort()
        for f in files:
            fullpath = os.path.join(thisdir, f)
            newfile = '{0}.{1}'.format(fullpath, sks['compress'])
            # TODO: add compressed tarball support.
            # However, I can't do this on memory-constrained systems for lrzip.
            # See: https://github.com/kata198/python-lrzip/issues/1
            with open(sks['logfile'], 'a') as f:
                f.write('===== {0} Now compressing {1} =====\n'.format(str(datetime.datetime.utcnow()), fullpath))
            if sks['compress'].lower() == 'gz':
                import gzip
                with open(fullpath, 'rb') as fh_in, gzip.open(newfile, 'wb') as fh_out:
                    fh_out.writelines(fh_in)
            elif sks['compress'].lower() == 'xz':
                import lzma
                with open(fullpath, 'rb') as fh_in, lzma.open(newfile, 'wb', preset = 9|lzma.PRESET_EXTREME) as fh_out:
                    fh_out.writelines(fh_in)
            elif sks['compress'].lower() == 'bz2':
                import bz2
                with open(fullpath, 'rb') as fh_in, bz2.open(newfile, 'wb') as fh_out:
                    fh_out.writelines(fh_in)
            elif sks['compress'].lower() == 'lrz':
                import lrzip
                with open(fullpath, 'rb') as fh_in, open(newfile, 'wb') as fh_out:
                    fh_out.write(lrzip.compress(fh_in.read()))
            os.remove(fullpath)
            if getpass.getuser() == 'root':
                uid = getpwnam(sks['user']).pw_uid
                gid = getgrnam(sks['group']).gr_gid
                os.chown(newfile, uid, gid)
    return()

def syncDB():
    if not sks['rsync']:
        return()
    cmd = ['rsync',
           '-a',
           '--delete',
           os.path.join(sks['destdir'], '.'),
           sks['rsync']]
    with open(sks['logfile'], 'a') as f:
        f.write('===== {0} Rsyncing to mirror =====\n'.format(str(datetime.datetime.utcnow())))
    with open(sks['logfile'], 'a') as f:
        subprocess.run(cmd, stdout = f, stderr = f)
    return()

def parseArgs():
    args = argparse.ArgumentParser(description = 'sksdump - a tool for dumping the SKS Database',
                                   epilog = 'brent s. || 2017 || https://square-r00t.net')
    args.add_argument('-d',
                      '--no-dump',
                      dest = 'nodump',
                      action = 'store_true',
                      help = 'Don\'t dump the SKS DB (default is to dump)')
    args.add_argument('-c',
                      '--no-compress',
                      dest = 'nocompress',
                      action = 'store_true',
                      help = 'Don\'t compress the DB dumps (default is to compress)')
    args.add_argument('-s',
                      '--no-sync',
                      dest = 'nosync',
                      action = 'store_true',
                      help = 'Don\'t sync the dumps to the remote server.')
    return(args)

def main():
    args = vars(parseArgs().parse_args())
    if getpass.getuser() not in ('root', sks['user']):
        exit('ERROR: You must be root or {0}!'.format(sks['user']))
    with open(sks['logfile'], 'a') as f:
        f.write('===== {0} STARTING ====='.format(str(datetime.datetime.utcnow())))
    if not args['nodump']:
        dumpDB()
    if not args['nocompress']:
        compressDB()
    if not args['nosync']:
        syncDB()
    with open(sks['logfile'], 'a') as f:
        f.write('===== {0} DONE ====='.format(str(datetime.datetime.utcnow())))


if __name__ == '__main__':
    main()
