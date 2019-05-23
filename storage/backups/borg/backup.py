#!/usr/bin/env python3

# TODO: https://borgbackup.readthedocs.io/en/latest/internals/frontends.html
# will they EVER release a public API? for now we'll just use subprocess since
# we import it for various prep stuff anyways.
# TODO: change loglevel of borg itself in subprocess to match the argparse?
# --debug, --info (same as -v/--verbose), --warning, --error, --critical
# TODO: modify config to add repo to cfg for init? or add new operation, "add"

import argparse
import configparser
import datetime
import json
import getpass
import logging
import logging.handlers
import os
import re
# TODO: use borg module directly?
import subprocess
import sys
# TODO: virtual env?
try:
    from lxml import etree
    has_lxml = True
except ImportError:
    import xml.etree.ElementTree as etree  # https://docs.python.org/3/library/xml.etree.elementtree.html
    has_lxml = False

try:
    import pymysql  # not stdlib; "python-pymysql" in Arch's AUR
    has_mysql = True
except ImportError:
    has_mysql = False
try:
    # https://www.freedesktop.org/software/systemd/python-systemd/journal.html#journalhandler-class
    from systemd import journal
    has_systemd = True
except ImportError:
    has_systemd = False

### LOG LEVEL MAPPINGS ###
loglvls = {'critical': logging.CRITICAL,
           'error': logging.ERROR,
           'warning': logging.WARNING,
           'info': logging.INFO,
           'debug': logging.DEBUG}


### THE GUTS ###
class Backup(object):
    def __init__(self, args):
        self.args = args
        ### DIRECTORIES ###
        if self.args['oper'] == 'backup':
            for d in (self.args['mysqldir'], self.args['stagedir']):
                os.makedirs(d, exist_ok = True, mode = 0o700)
        if self.args['oper'] == 'restore':
            self.args['target_dir'] = os.path.abspath(os.path.expanduser(
                                                    self.args['target_dir']))
            os.makedirs(os.path.dirname(self.args['oper']),
                        exist_ok = True,
                        mode = 0o700)
        ### LOGGING ###
        # Thanks to:
        # https://web.archive.org/web/20170726052946/http://www.lexev.org/en/2013/python-logging-every-day/
        # https://stackoverflow.com/a/42604392
        # https://plumberjack.blogspot.com/2010/10/supporting-alternative-formatting.html
        # and user K900_ on r/python for entertaining my very silly question.
        self.logger = logging.getLogger(__name__)
        self.logger.setLevel(loglvls[self.args['loglevel']])
        _logfmt = logging.Formatter(
                fmt = ('{levelname}:{name}: {message} ({asctime}; '
                       '{filename}:{lineno})'),
                style = '{',
                datefmt = '%Y-%m-%d %H:%M:%S')
        _journalfmt = logging.Formatter(
                fmt = '{levelname}:{name}: {message} ({filename}:{lineno})',
                style = '{',
                datefmt = '%Y-%m-%d %H:%M:%S')
        handlers = []
        if self.args['disklog']:
            os.makedirs(os.path.dirname(self.args['logfile']),
                        exist_ok = True,
                        mode = 0o700)
            # TODO: make the constraints for rotation in config?
            handlers.append(
                    logging.handlers.RotatingFileHandler(self.args['logfile'],
                                                         encoding = 'utf8',
                                                         maxBytes = 100000,
                                                         backupCount = 1))
        if self.args['verbose']:
            handlers.append(logging.StreamHandler())
        if has_systemd:
            h = journal.JournalHandler()
            h.setFormatter(_journalfmt)
            h.setLevel(loglvls[self.args['loglevel']])
            self.logger.addHandler(h)
        for h in handlers:
            h.setFormatter(_logfmt)
            h.setLevel(loglvls[self.args['loglevel']])
            self.logger.addHandler(h)
        self.logger.debug('BEGIN INITIALIZATION')
        ### CONFIG ###
        if not os.path.isfile(self.args['cfgfile']):
            self.logger.error(
                    '{0} does not exist'.format(self.args['cfgfile']))
            exit(1)
        with open(self.args['cfgfile'], 'r') as f:
            self.cfg = json.loads(f.read())
        ### END LOGGING ###
        ### ARGS CLEANUP ###
        self.logger.debug('VARS (before args cleanup): {0}'.format(vars()))
        self.args['repo'] = [i.strip() for i in self.args['repo'].split(',')]
        if 'all' in self.args['repo']:
            self.args['repo'] = list(self.cfg['repos'].keys())
        for r in self.args['repo'][:]:
            if r == 'all':
                self.args['repo'].remove(r)
            elif r not in self.cfg['repos'].keys():
                self.logger.warning(
                        'Repository {0} is not configured; skipping.'.format(
                                r))
                self.args['repo'].remove(r)
        self.logger.debug('VARS (after args cleanup): {0}'.format(vars()))
        self.logger.debug('END INITIALIZATION')
        ### CHECK ENVIRONMENT ###
        # If we're running from cron, we want to print errors to stdout.
        if os.isatty(sys.stdin.fileno()):
            self.cron = False
        else:
            self.cron = True
        ### END INIT ###

    def cmdExec(self, cmd, stdoutfh = None):
        self.logger.debug('Running command: {0}'.format(' '.join(cmd)))
        if self.args['dryrun']:
            return ()  # no-op
        if stdoutfh:
            _cmd = subprocess.run(cmd,
                                  stdout = stdoutfh,
                                  stderr = subprocess.PIPE)
        else:
            _cmd = subprocess.run(cmd,
                                  stdout = subprocess.PIPE,
                                  stderr = subprocess.PIPE)
            _out = _cmd.stdout.decode('utf-8').strip()
        _err = _cmd.stderr.decode('utf-8').strip()
        _returncode = _cmd.returncode
        if _returncode != 0:
            self.logger.error('STDERR: ({1})\n{0}'.format(_err, ' '.join(cmd)))
        if _err != '' and self.cron:
            self.logger.warning(
                    'Command {0} failed: {1}'.format(' '.join(cmd), _err))
        return()

    def createRepo(self):
        _env = os.environ.copy()
        _env['BORG_RSH'] = self.cfg['config']['ctx']
        for r in self.args['repo']:
            self.logger.info('[{0}]: BEGIN INITIALIZATION'.format(r))
            _cmd = ['borg',
                    'init',
                    '-v',
                    '{0}@{1}:{2}'.format(self.cfg['config']['user'],
                                         self.cfg['config']['host'],
                                         r)]
            _env['BORG_PASSPHRASE'] = self.cfg['repos'][r]['password']
            # We don't use self.cmdExec() here either because
            # again, custom env, etc.
            self.logger.debug('VARS: {0}'.format(vars()))
            if not self.args['dryrun']:
                _out = subprocess.run(_cmd,
                                      env = _env,
                                      stdout = subprocess.PIPE,
                                      stderr = subprocess.PIPE)
                _stdout = _out.stdout.decode('utf-8').strip()
                _stderr = _out.stderr.decode('utf-8').strip()
                _returncode = _out.returncode
                self.logger.debug('[{0}]: (RESULT) {1}'.format(r, _stdout))
                # sigh. borg uses stderr for verbose output.
                self.logger.debug('[{0}]: STDERR: ({2})\n{1}'.format(r,
                                                                     _stderr,
                                                                     ' '.join(
                                                                             _cmd)))
                if _returncode != 0:
                    self.logger.error(
                            '[{0}]: FAILED: {1}'.format(r, ' '.join(_cmd)))
                if _err != '' and self.cron and _returncode != 0:
                    self.logger.warning(
                            'Command {0} failed: {1}'.format(' '.join(cmd),
                                                             _err))
            del (_env['BORG_PASSPHRASE'])
            self.logger.info('[{0}]: END INITIALIZATION'.format(r))
        return()

    def create(self):
        # TODO: support "--strip-components N"?
        _env = os.environ.copy()
        _env['BORG_RSH'] = self.cfg['config']['ctx']
        self.logger.info('START: backup')
        for r in self.args['repo']:
            self.logger.info('[{0}]: BEGIN BACKUP'.format(r))
            if 'prep' in self.cfg['repos'][r].keys():
                for prep in self.cfg['repos'][r]['prep']:
                    self.logger.info(
                            '[{0}]: Running prepfunc {1}'.format(r, prep))
                    eval('self.{0}'.format(
                            prep))  # I KNOW, IT'S TERRIBLE. so sue me.
                    self.logger.info(
                            '[{0}]: Finished prepfunc {1}'.format(r, prep))
            _cmd = ['borg',
                    'create',
                    '-v', '--stats',
                    '--compression', 'lzma,9']
            if 'excludes' in self.cfg['repos'][r].keys():
                for e in self.cfg['repos'][r]['excludes']:
                    _cmd.extend(['--exclude', e])
            _cmd.append('{0}@{1}:{2}::{3}'.format(self.cfg['config']['user'],
                                                  self.cfg['config']['host'],
                                                  r,
                                                  self.args['archive']))
            for p in self.cfg['repos'][r]['paths']:
                _cmd.append(p)
            _env['BORG_PASSPHRASE'] = self.cfg['repos'][r]['password']
            self.logger.debug('VARS: {0}'.format(vars()))
            # We don't use self.cmdExec() here because we want to explicitly
            # pass the env and format the log line differently.
            self.logger.debug(
                    '[{0}]: Running command: {1}'.format(r, ' '.join(_cmd)))
            if not self.args['dryrun']:
                _out = subprocess.run(_cmd,
                                      env = _env,
                                      stdout = subprocess.PIPE,
                                      stderr = subprocess.PIPE)
                _stdout = _out.stdout.decode('utf-8').strip()
                _stderr = _out.stderr.decode('utf-8').strip()
                _returncode = _out.returncode
                self.logger.debug('[{0}]: (RESULT) {1}'.format(r, _stdout))
                self.logger.error('[{0}]: STDERR: ({2})\n{1}'.format(r,
                                                                     _stderr,
                                                                     ' '.join(
                                                                             _cmd)))
                if _returncode != 0:
                    self.logger.error(
                            '[{0}]: FAILED: {1}'.format(r, ' '.join(_cmd)))
                if _stderr != '' and self.cron and _returncode != 0:
                    self.logger.warning(
                            'Command {0} failed: {1}'.format(' '.join(_cmd),
                                                             _stderr))
                del (_env['BORG_PASSPHRASE'])
            self.logger.info('[{0}]: END BACKUP'.format(r))
        self.logger.info('END: backup')
        return()

    def restore(self):
        # TODO: support "--strip-components N"?
        # TODO: support add'l args?
        # https://borgbackup.readthedocs.io/en/stable/usage/extract.html
        _env = os.environ.copy()
        _env['BORG_RSH'] = self.cfg['config']['ctx']
        self.logger.info('START: restore')
        for r in self.args['repo']:
            self.logger.info('[{0}]: BEGIN RESTORE'.format(r))
            _cmd = ['borg',
                    'extract',
                    '-v']
            # if 'excludes' in self.cfg['repos'][r].keys():
            #     for e in self.cfg['repos'][r]['excludes']:
            #         _cmd.extend(['--exclude', e])
            _cmd.append('{0}@{1}:{2}::{3}'.format(self.cfg['config']['user'],
                                                  self.cfg['config']['host'],
                                                  r,
                                                  self.args['archive']))
            _cmd.append(os.path.abspath(self.args['target_dir']))
            # TODO: support specific path inside archive?
            # if so, append path(s) here.
            _env['BORG_PASSPHRASE'] = self.cfg['repos'][r]['password']
            self.logger.debug('VARS: {0}'.format(vars()))
            # We don't use self.cmdExec() here because we want to explicitly
            # pass the env and format the log line differently.
            self.logger.debug(
                    '[{0}]: Running command: {1}'.format(r, ' '.join(_cmd)))
            if not self.args['dryrun']:
                _out = subprocess.run(_cmd,
                                      env = _env,
                                      stdout = subprocess.PIPE,
                                      stderr = subprocess.PIPE)
                _stdout = _out.stdout.decode('utf-8').strip()
                _stderr = _out.stderr.decode('utf-8').strip()
                _returncode = _out.returncode
                self.logger.debug('[{0}]: (RESULT) {1}'.format(r, _stdout))
                self.logger.error('[{0}]: STDERR: ({2})\n{1}'.format(r,
                                                                     _stderr,
                                                                     ' '.join(
                                                                             _cmd)))
                if _returncode != 0:
                    self.logger.error(
                            '[{0}]: FAILED: {1}'.format(r, ' '.join(_cmd)))
                if _stderr != '' and self.cron and _returncode != 0:
                    self.logger.warning(
                            'Command {0} failed: {1}'.format(' '.join(_cmd),
                                                             _stderr))
                del (_env['BORG_PASSPHRASE'])
            self.logger.info('[{0}]: END RESTORE'.format(r))
        self.logger.info('END: restore')
        return()

    def listRepos(self):
        print('\n\033[1mCurrently configured repositories are:\033[0m\n')
        print('\t{0}\n'.format(', '.join(self.cfg['repos'].keys())))
        if self.args['verbose']:
            print('\033[1mDETAILS:\033[0m\n')
            for r in self.args['repo']:
                print('\t\033[1m{0}:\033[0m\n\t\t\033[1mPath(s):\033[0m\t'.format(
                                r.upper()), end = '')
                for p in self.cfg['repos'][r]['paths']:
                    print(p, end = ' ')
                if 'prep' in self.cfg['repos'][r].keys():
                    print('\n\t\t\033[1mPrep:\033[0m\t\t', end = '')
                    for p in self.cfg['repos'][r]['prep']:
                        print(p, end = ' ')
                if 'excludes' in self.cfg['repos'][r].keys():
                    print('\n\t\t\033[1mExclude(s):\033[0m\t', end = '')
                    for p in self.cfg['repos'][r]['excludes']:
                        print(p, end = ' ')
                print('\n')
        return()

    def printer(self):
        # TODO: better alignment. https://stackoverflow.com/a/5676884
        _results = self.lister()
        if not self.args['archive']:  # It's a listing of archives
            print('\033[1mREPO:\tSNAPSHOT:\t\tTIMESTAMP:\033[0m\n')
            for r in _results.keys():
                print(r, end = '')
                for line in _results[r]:
                    _snapshot = line.split()
                    print('\t{0}\t\t{1}'.format(_snapshot[0],
                                                ' '.join(_snapshot[1:])))
                print()
        else:  # It's a listing inside an archive
            if self.args['verbose']:
                _fields = ['REPO:', 'PERMS:', 'OWNERSHIP:', 'SIZE:', 'TIMESTAMP:', 'PATH:']
                for r in _results.keys():
                    print('\033[1m{0}\t{1}\033[0m'.format(_fields[0], r))
                    # https://docs.python.org/3/library/string.html#formatspec
                    print('{0[1]:<15}\t{0[2]:<15}\t{0[3]:<15}\t{0[4]:<24}\t{0[5]:<15}'.format(_fields))
                    for line in _results[r]:
                        _fline = line.split()
                        _perms = _fline[0]
                        _ownership = '{0}:{1}'.format(_fline[1], _fline[2])
                        _size = _fline[3]
                        _time = ' '.join(_fline[4:7])
                        _path = ' '.join(_fline[7:])
                        print('{0:<15}\t{1:<15}\t{2:<15}\t{3:<24}\t{4:<15}'.format(_perms,
                                                                                   _ownership,
                                                                                   _size,
                                                                                   _time,
                                                                                   _path))
            else:
                print('\033[1mREPO:\tPATH:\033[0m\n')
                for r in _results.keys():
                    print(r, end = '')
                    for line in _results[r]:
                        _fline = line.split()
                        print('\t{0}'.format(' '.join(_fline[7:])))
        return()

    def lister(self):
        output = {}
        _env = os.environ.copy()
        self.logger.debug('START: lister')
        _env['BORG_RSH'] = self.cfg['config']['ctx']
        for r in self.args['repo']:
            if self.args['archive']:
                _cmd = ['borg',
                        'list',
                        '{0}@{1}:{2}::{3}'.format(self.cfg['config']['user'],
                                                  self.cfg['config']['host'],
                                                  r,
                                                  self.args['archive'])]
            else:
                _cmd = ['borg',
                        'list',
                        '{0}@{1}:{2}'.format(self.cfg['config']['user'],
                                             self.cfg['config']['host'],
                                             r)]
            _env['BORG_PASSPHRASE'] = self.cfg['repos'][r]['password']
            if not self.args['dryrun']:

                _out = subprocess.run(_cmd,
                                      env = _env,
                                      stdout = subprocess.PIPE,
                                      stderr = subprocess.PIPE)
                _stdout = [i.strip() for i in _out.stdout.decode('utf-8').splitlines()]
                _stderr = _out.stderr.decode('utf-8').strip()
                _returncode = _out.returncode
                output[r] = _stdout
                self.logger.debug('[{0}]: (RESULT) {1}'.format(r,
                                                               '\n'.join(_stdout)))
                if _returncode != 0:
                    self.logger.error('[{0}]: STDERR: ({2}) ({1})'.format(r,
                                                                          _stderr,
                                                                          ' '.join(_cmd)))
                if _stderr != '' and self.cron and _returncode != 0:
                    self.logger.warning(
                            'Command {0} failed: {1}'.format(' '.join(cmd), _err))
            del(_env['BORG_PASSPHRASE'])
            if not self.args['archive']:
                if self.args['numlimit'] > 0:
                    if self.args['old']:
                        output[r] = output[r][:self.args['numlimit']]
                    else:
                        output[r] = list(reversed(output[r]))[:self.args['numlimit']]
            if self.args['invert']:
                output[r] = reversed(output[r])
        self.logger.debug('END: lister')
        return(output)


def printMoarHelp():
    _helpstr = ('\n\tNOTE: Sorting only applies to listing archives, NOT the contents!\n\n'
                'In order to efficiently display results, there are several options to handle it. '
                'Namely, these are:\n\n\t\t'
                    '-s/--sort [direction]\n\t\t'
                    '-l/--limit [number]\n\t\t'
                    '-x/--invert\n\n'
                'For example, if you want to list the 5 most recently *taken* snapshots, you would use:\n\n\t\t'
                    '-l 5\n\n'
                'If you would want those SAME results SORTED in the reverse order (i.e. the 5 most recently '
                'taken snapshots sorted from newest to oldest), then it would be: \n\n\t\t'
                    '-l 5 -x\n\n'
                'Lastly, if you wanted to list the 7 OLDEST TAKEN snapshots in reverse order '
                '(that is, sorted from newest to oldest), that\'d be:\n\n\t\t'
                    '-o -l 7 -x\n')
    print(_helpstr)
    exit(0)


def parseArgs():
    ### DEFAULTS ###
    _date = datetime.datetime.now().strftime("%Y_%m_%d.%H_%M")
    _logfile = '/var/log/borg/{0}'.format(_date)
    _mysqldir = os.path.abspath(
            os.path.join(os.path.expanduser('~'),
                         '.bak',
                         'mysql'))
    _stagedir = os.path.abspath(
            os.path.join(os.path.expanduser('~'),
                         '.bak',
                         'misc'))
    _cfgfile = os.path.abspath(
            os.path.join(os.path.expanduser('~'),
                         '.config',
                         'optools',
                         'backup.xml'))
    _defloglvl = 'info'
    ######
    args = argparse.ArgumentParser(description = 'Backups manager',
                                   epilog = ('TIP: this program has context-specific help. '
                                             'e.g. try "%(prog)s list --help"'))
    args.add_argument('-c', '--config',
                      dest = 'cfgfile',
                      default = _cfgfile,
                      help = (
                          'The path to the config file. '
                          'Default: \033[1m{0}\033[0m'.format(_cfgfile)))
    args.add_argument('-Ll', '--loglevel',
                      dest = 'loglevel',
                      default = _defloglvl,
                      choices = list(loglvls.keys()),
                      help = (
                          'The level of logging to perform. \033[1mWARNING:\033[0m \033[1mdebug\033[0m will '
                          'log VERY sensitive information such as passwords! '
                          'Default: \033[1m{0}\033[0m'.format(_defloglvl)))
    args.add_argument('-Ld', '--log-to-disk',
                      dest = 'disklog',
                      action = 'store_true',
                      help = (
                          'If specified, log to a specific file (-Lf/--logfile) instead of the system logger.'))
    args.add_argument('-Lf', '--logfile',
                      dest = 'logfile',
                      default = _logfile,
                      help = (
                          'The path to the logfile, only used if -Ld/--log-to-disk is specified. '
                          'Default: \033[1m{0}\033[0m (dynamic)').format(_logfile))
    args.add_argument('-v', '--verbose',
                      dest = 'verbose',
                      action = 'store_true',
                      help = ('If specified, log messages will be printed to STDERR in addition to the other '
                              'configured log system(s), and verbosity for printing functions is increased. '
                              '\033[1mWARNING:\033[0m This may display VERY sensitive information such as passwords!'))
    ### ARGS FOR ALL OPERATIONS ###
    commonargs = argparse.ArgumentParser(add_help = False)
    commonargs.add_argument('-r', '--repo',
                            dest = 'repo',
                            default = 'all',
                            help = ('The repository to perform the operation for. '
                                    'The default is \033[1mall\033[0m, a special value that specifies all known '
                                    'repositories. Can also accept a comma-separated list.'))
    fileargs = argparse.ArgumentParser(add_help = False)
    fileargs.add_argument('-a', '--archive',
                          default = _date,
                          dest = 'archive',
                          help = ('The name of the archive/snapshot. '
                                  'Default: \033[1m{0}\033[0m (dynamic)').format(_date))
    remoteargs = argparse.ArgumentParser(add_help = False)
    remoteargs.add_argument('-d', '--dry-run',
                            dest = 'dryrun',
                            action = 'store_true',
                            help = ('Act as if we are performing tasks, but none will actually be executed '
                                    '(useful for testing logging)'))
    ### OPERATIONS ###
    subparsers = args.add_subparsers(help = 'Operation to perform',
                                     dest = 'oper')
    backupargs = subparsers.add_parser('backup',
                                       help = 'Perform a backup.',
                                       parents = [commonargs,
                                                  remoteargs,
                                                  fileargs])
    listargs = subparsers.add_parser('list',
                                     help = 'List available backups.',
                                     parents = [commonargs, remoteargs])
    listrepoargs = subparsers.add_parser('listrepos',
                                         help = ('List availabile/configured repositories.'),
                                         parents = [commonargs])
    initargs = subparsers.add_parser('init',
                                     help = 'Initialise a repository.',
                                     parents = [commonargs, remoteargs])
    rstrargs = subparsers.add_parser('restore',
                                     help = ('Restore ("extract") an archive.'),
                                     parents = [commonargs,
                                                remoteargs,
                                                fileargs])
    cvrtargs = subparsers.add_parser('convert',
                                     help = ('Convert the legacy JSON format to the new XML format and quit'))
    ### OPERATION-SPECIFIC OPTIONS ###
    # CREATE ("backup") #
    backupargs.add_argument('-s', '--stagedir',
                            default = _stagedir,
                            dest = 'stagedir',
                            help = ('The directory used for staging temporary files, if necessary. '
                                    'Default: \033[1m{0}\033[0m').format(_stagedir))
    backupargs.add_argument('-m', '--mysqldir',
                            default = _mysqldir,
                            dest = 'mysqldir',
                            help = ('The path to where MySQL dumps should go. '
                                    'Default: \033[1m{0}\033[0m').format(_mysqldir))
    # DISPLAY/OUTPUT ("list") #
    listargs.add_argument('-a', '--archive',
                          dest = 'archive',
                          default = False,
                          help = 'If specified, will list the *contents* of the given archive name.')
    listargs.add_argument('-l', '--limit',
                          dest = 'numlimit',
                          type = int,
                          default = '5',
                          help = ('If specified, constrain the outout to this number of results each repo. '
                                  'Default is \033[1m5\033[0m, use 0 for unlimited. See \033[1m-H/--list-help\033[0m'))
    listargs.add_argument('-s', '--sort',
                          dest = 'sortby',
                          choices = ['newest', 'oldest'],
                          default = 'oldest',
                          help = ('The order to sort the results by. See \033[1m-H/--list-help\033[0m. '
                                  'Default: \033[1moldest\033[0m'))
    listargs.add_argument('-x', '--invert',
                          dest = 'invert',
                          action = 'store_true',
                          help = 'Invert the order of results. See \033[1m-H/--list-help\033[0m.')
    listargs.add_argument('-o', '--old',
                          dest = 'old',
                          action = 'store_true',
                          help = ('Instead of grabbing the latest results, grab the earliest results. This differs '
                                  'from \033[1m-s/--sort\033[0m. See \033[1m-H/--list-help\033[0m.'))
    listargs.add_argument('-H', '--list-help',
                          dest = 'moarhelp',
                          action = 'store_true',
                          help = ('Print extended information about how to '
                                  'manage the output of listing and exit.'))
    ## EXTRACT ("restore")
    rstrargs.add_argument('-t', '--target',
                          required = True,
                          dest = 'target_dir',
                          help = ('The path to the directory where the restore should be dumped to. It is '
                                  'recommended to NOT restore to the same directory that the archive is taken from.'))
    return (args)

def convertConf(cfgfile):
    try:
        with open(cfgfile, 'r') as f:
            oldcfg = json.load(f)
    except json.decoder.JSONDecodeError:
        # It's not JSON. It's either already XML or invalid config.
        return(cfgfile)
    # Switched from JSON to XML, so we need to do some basic conversion.
    newfname = re.sub('(\.json)?$', '.xml', os.path.basename(cfgfile))
    newcfg = os.path.join(os.path.dirname(cfgfile),
                          newfname)
    if os.path.exists(newcfg):
        # Do nothing. We don't want to overwrite an existing config
        # and we'll assume it's an already-done conversion.
        return(newcfg)
    print(('It appears that you are still using the legacy JSON format. '
           'We will attempt to convert it to the new XML format ({0}) but it may '
           'require modifications, especially if you are using any prep functions as those are not '
           'converted automatically. See sample.config.xml for an example of this.').format(newcfg))
    cfg = etree.Element('borg')
    # The old format only supported one server.
    server = etree.Element('server')
    server.attrib['target'] = oldcfg['config']['host']
    server.attrib['rsh'] = oldcfg['config']['ctx']
    server.attrib['user'] = oldcfg['config']['user']
    for r in oldcfg['repos']:
        repo = etree.Element('repo')
        repo.attrib['name'] = r
        repo.attrib['password'] = oldcfg['repos'][r]['password']
        for p in oldcfg['repos'][r]['paths']:
            path = etree.Element('path')
        server.append(repo)
    # Build the full XML spec.
    namespaces = {'borg': 'http://git.square-r00t.net/OpTools/tree/storage/backups/borg/',
                  'xsi': 'http://www.w3.org/2001/XMLSchema-instance'}
    xsi = {('{http://www.w3.org/2001/'
            'XMLSchema-instance}schemaLocation'): ('http://git.square-r00t.net/OpTools/plain/'
                                                   'storage/backups/borg/config.xsd')}
    if has_lxml:
        genname = 'LXML (http://lxml.de/)'
        root = etree.Element('borg', nsmap = namespaces, attrib = xsi)
    else:
        genname = 'Python stdlib "xml" module'
        for ns in namespaces.keys():
            etree.register_namespace(ns, namespaces[ns])
        root = etree.Element('borg')
    fromstr = cfgfile
    root.append(etree.Comment(
            ('Generated by {0} on {1} from {2} via {3}').format(sys.argv[0],
                                                                datetime.datetime.now(),
                                                                fromstr,
                                                                genname)))
    root.append(etree.Comment('THIS FILE CONTAINS SENSITIVE INFORMATION. SHARE/SCRUB WISELY.'))
    for x in cfg:
        root.append(x)
    # Write out the file to disk.
    if has_lxml:
        xml = etree.ElementTree(root)
        with open(newcfg, 'wb') as f:
            xml.write(f,
                      xml_declaration = True,
                      encoding = 'utf-8',
                      pretty_print = True)
    else:
        import xml.dom.minidom
        xmlstr = etree.tostring(root, encoding = 'utf-8')
        # holy cats, the xml module sucks.
        nsstr = ''
        for ns in namespaces.keys():
            nsstr += ' xmlns:{0}="{1}"'.format(ns, namespaces[ns])
        for x in xsi.keys():
            xsiname = x.split('}')[1]
            nsstr += ' xsi:{0}="{1}"'.format(xsiname, xsi[x])
        outstr = xml.dom.minidom.parseString(xmlstr).toprettyxml(indent = '  ').splitlines()
        outstr[0] = '<?xml version=\'1.0\' encoding=\'utf-8\'?>'
        outstr[1] = '<borg{0}>'.format(nsstr)
        with open(newcfg, 'w') as f:
            f.write('\n'.join(outstr))
    # Return the new config's path.
    return(newcfg)


def main():
    rawargs = parseArgs()
    parsedargs = rawargs.parse_args()
    args = vars(parsedargs)
    args['cfgfile'] = os.path.abspath(os.path.expanduser(args['cfgfile']))
    if not args['oper']:
        rawargs.print_help()
        exit(0)
    if 'moarhelp' in args.keys() and args['moarhelp']:
        printMoarHelp()
    if args['oper'] == 'convert':
        convertConf(args['cfgfile'])
        return()
    else:
        try:
            with open(args['cfgfile'], 'r') as f:
                json.load(f)
                args['cfgfile'] = convertConf(args['cfgfile'])
        except json.decoder.JSONDecodeError:
            # It's not JSON. It's either already XML or invalid config.
            pass
    # The "Do stuff" part
    bak = Backup(args)
    if args['oper'] == 'list':
        bak.printer()
    elif args['oper'] == 'listrepos':
        bak.listRepos()
    elif args['oper'] == 'backup':
        bak.create()
    elif args['oper'] == 'init':
        bak.createRepo()
    elif args['oper'] == 'restore':
        bak.restore()
    return()


if __name__ == '__main__':
    main()
