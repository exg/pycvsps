# Mercurial built-in replacement for cvsps.
#
# Copyright 2008, Frank Kingswood <frank@kingswood-consulting.co.uk>
#
# This software may be used and distributed according to the terms of the
# GNU General Public License version 2 or any later version.

import functools
import os
import os.path
import pickle
import re
import subprocess
import sys
from optparse import OptionParser, SUPPRESS_HELP
from .dateutil import datestr, parsedate

def ellipsis(text, maxlength=400):
    if len(text) <= maxlength:
        return text
    else:
        return "%s..." % (text[:maxlength - 3])

def _(s):
    return s

class logentry(object):
    '''Class logentry has the following attributes:
        .author    - author name as CVS knows it
        .branch    - name of branch this revision is on
        .branches  - revision tuple of branches starting at this revision
        .comment   - commit message
        .commitid  - CVS commitid or None
        .date      - the commit date as a (time, tz) tuple
        .dead      - true if file revision is dead
        .file      - Name of file
        .lines     - a tuple (+lines, -lines) or None
        .parent    - Previous revision of this entry
        .rcs       - name of file as returned from CVS
        .revision  - revision number as tuple
        .tags      - list of tags on the file
        .synthetic - is this a synthetic "file ... added on ..." revision?
        .mergepoint - the branch that has been merged from (if present in
                      rlog output) or None
        .branchpoints - the branches that start at the current entry or empty
    '''
    def __init__(self, **entries):
        self.synthetic = False
        self.__dict__.update(entries)

    def __repr__(self):
        items = ("%s=%r"%(k, self.__dict__[k]) for k in sorted(self.__dict__))
        return "%s(%s)"%(type(self).__name__, ", ".join(items))

class logerror(Exception):
    pass

def parse_revision(revision):
    return tuple(map(int, revision.split('.')))

def getrepopath(cvspath):
    """Return the repository path from a CVS path.

    >>> getrepopath('/foo/bar')
    '/foo/bar'
    >>> getrepopath('c:/foo/bar')
    '/foo/bar'
    >>> getrepopath(':pserver:10/foo/bar')
    '/foo/bar'
    >>> getrepopath(':pserver:10c:/foo/bar')
    '/foo/bar'
    >>> getrepopath(':pserver:/foo/bar')
    '/foo/bar'
    >>> getrepopath(':pserver:c:/foo/bar')
    '/foo/bar'
    >>> getrepopath(':pserver:truc@foo.bar:/foo/bar')
    '/foo/bar'
    >>> getrepopath(':pserver:truc@foo.bar:c:/foo/bar')
    '/foo/bar'
    >>> getrepopath('user@server/path/to/repository')
    '/path/to/repository'
    """
    # According to CVS manual, CVS paths are expressed like:
    # [:method:][[user][:password]@]hostname[:[port]]/path/to/repository
    #
    # CVSpath is splitted into parts and then position of the first occurrence
    # of the '/' char after the '@' is located. The solution is the rest of the
    # string after that '/' sign including it

    parts = cvspath.split(':')
    atposition = parts[-1].find('@')
    start = 0

    if atposition != -1:
        start = atposition

    repopath = parts[-1][parts[-1].find('/', start):]
    return repopath

def rcs_path(path):
    dname, fname = os.path.split(path)
    comps = []
    if dname:
        while True:
            head, tail = os.path.split(dname)
            if dname == head:
                break
            dname = head
            if tail and tail != 'Attic':
                comps.append(tail)
        comps.reverse()
    comps.append(fname)
    return os.path.join(*comps)

def build_prefix(root, repository):
    repository = os.path.normpath(repository)
    if repository == '.':
        repository = ''
    if root:
        path = os.path.normpath(getrepopath(root))
        if repository:
            prefix = os.path.join(path, repository)
        else:
            prefix = path
    else:
        prefix = repository
    return prefix + os.sep

def createlog(ui, directory=None, root="", rlog=True, cache=None):
    '''Collect the CVS rlog'''

    # Because we store many duplicate commit log messages, reusing strings
    # saves a lot of memory and pickle storage space.
    _scache = {}
    def scache(s):
        "return a shared version of a string"
        return _scache.setdefault(s, s)

    ui.status(_('collecting CVS rlog\n'))

    log = []      # list of logentry objects containing the CVS state

    # patterns to match in CVS (r)log output, by state of use
    re_00 = re.compile(r'RCS file: (.+)$')
    re_01 = re.compile(r'cvs \[r?log aborted\]: (.+)$')
    re_02 = re.compile(r'cvs (r?log|server): (.+)\n$')
    re_03 = re.compile(r"(Cannot access.+CVSROOT)|"
                       r"(can't create temporary directory.+)$")
    re_10 = re.compile(r'Working file: (.+)$')
    re_20 = re.compile(r'symbolic names:')
    re_30 = re.compile(r'\t(.+): ([\d.]+)$')
    re_31 = re.compile(r'----------------------------$')
    re_32 = re.compile(r'======================================='
                       r'======================================$')
    re_50 = re.compile(r'revision ([\d.]+)(\s+locked by:\s+.+;)?$')
    re_60 = re.compile(r'date:\s+(.+);\s+author:\s+(.+);\s+state:\s+(.+?);'
                       r'(\s+lines:\s+(\+\d+)?\s+(-\d+)?;)?'
                       r'(\s+commitid:\s+([^;]+);)?'
                       r'(.*mergepoint:\s+([^;]+);)?')
    re_70 = re.compile(r'branches: (.+);$')

    file_added_re = re.compile(r'file [^/]+ was (initially )?added on branch')

    if directory is None:
        # Current working directory

        # Get the real directory in the repository
        try:
            with open(os.path.join('CVS', 'Repository'), encoding='ascii') as f:
                directory = f.read().strip()
        except IOError:
            raise logerror(_('not a CVS sandbox'))

        # Use the Root file in the sandbox, if it exists
        try:
            with open(os.path.join('CVS', 'Root'), encoding='ascii') as f:
                root = f.read().strip()
        except IOError:
            pass

    if not root:
        root = os.environ.get('CVSROOT', '')

    # read log cache if one exists
    oldlog = []
    date = None

    update_log = cache in ('write', 'update')

    if cache:
        cachedir = os.path.expanduser('~/.pycvsps')
        if not os.path.exists(cachedir):
            os.mkdir(cachedir)

        # The cvsps cache pickle needs a uniquified name, based on the
        # repository location. The address may have all sort of nasties
        # in it, slashes, colons and such. So here we take just the
        # alphanumeric characters, concatenated in a way that does not
        # mix up the various components, so that
        #    :pserver:user@server:/path
        # and
        #    /pserver/user/server/path
        # are mapped to different cache file names.
        cachefile = root.split(":") + [directory, "cache"]
        cachefile = ['-'.join(re.findall(r'\w+', s)) for s in cachefile if s]
        cachefile = os.path.join(cachedir,
                                 '.'.join([s for s in cachefile if s]))

    if cache in ('read', 'update'):
        try:
            ui.note(_('reading cvs log cache %s\n') % cachefile)
            oldlog = pickle.load(open(cachefile, 'rb'))
            ui.note(_('cache has %d log entries\n') % len(oldlog))
        except Exception as e:
            ui.note(_('error reading cache: %r\n') % e)
            update_log = True

        if oldlog:
            date = oldlog[-1].date    # last commit date as a (time,tz) tuple
            date = datestr(date, '%Y/%m/%d %H:%M:%S %1%2')

    if not update_log:
        return oldlog

    # build the CVS commandline
    cmd = ['cvs', '-q']
    if root:
        cmd.append('-d%s' % root)
    cmd.append(['log', 'rlog'][rlog])
    if date:
        # no space between option and date string
        cmd.append('-d>%s' % date)
    cmd.append(directory)
    prefix = build_prefix(root, directory)

    # state machine begins here
    tags = {}     # dictionary of revisions on current file with their tags
    branchmap = {} # mapping between branch names and revision numbers
    rcsmap = {}
    state = 0
    store = False # set when a new record can be appended

    ui.note(_("running %s\n") % (' '.join(cmd)))
    ui.debug("prefix=%r directory=%r root=%r\n" % (prefix, directory, root))

    pfp = subprocess.Popen(cmd, stdout=subprocess.PIPE)
    peek = pfp.stdout.readline().decode('latin-1')
    while True:
        line = peek
        if line == '':
            break
        peek = pfp.stdout.readline().decode('latin-1')
        if line.endswith('\n'):
            line = line[:-1]
        #ui.debug('state=%d line=%r\n' % (state, line))

        if state == 0:
            # initial state, consume input until we see 'RCS file'
            match = re_00.match(line)
            if match:
                rcs = match.group(1)
                tags = {}
                if rlog:
                    filename = os.path.normpath(rcs[:-2])
                    if filename.startswith(prefix):
                        filename = filename[len(prefix):]
                        filename = rcs_path(filename)
                        state = 2
                    continue
                state = 1
                continue
            match = re_01.match(line)
            if match:
                raise logerror(match.group(1))
            match = re_02.match(line)
            if match:
                raise logerror(match.group(2))
            if re_03.match(line):
                raise logerror(line)

        elif state == 1:
            # expect 'Working file' (only when using log instead of rlog)
            match = re_10.match(line)
            assert match, _('RCS file must be followed by working file')
            filename = os.path.normpath(match.group(1))
            state = 2

        elif state == 2:
            # expect 'symbolic names'
            if re_20.match(line):
                branchmap = {}
                state = 3

        elif state == 3:
            # read the symbolic names and store as tags
            match = re_30.match(line)
            if match:
                rev = parse_revision(match.group(2))

                # Convert magic branch number to an odd-numbered one
                revn = len(rev)
                if revn > 3 and (revn % 2) == 0 and rev[-2] == 0:
                    rev = rev[:-2] + rev[-1:]

                if rev not in tags:
                    tags[rev] = []
                tags[rev].append(match.group(1))
                branchmap[match.group(1)] = match.group(2)

            elif re_31.match(line):
                state = 5
            elif re_32.match(line):
                state = 0

        elif state == 4:
            # expecting '------' separator before first revision
            if re_31.match(line):
                state = 5
            else:
                assert not re_32.match(line), _('must have at least '
                                                'some revisions')

        elif state == 5:
            # expecting revision number and possibly (ignored) lock indication
            # we create the logentry here from values stored in states 0 to 4,
            # as this state is re-entered for subsequent revisions of a file.
            match = re_50.match(line)
            assert match, _('expected revision number')
            e = logentry(rcs=scache(rcs),
                         file=scache(filename),
                         revision=parse_revision(match.group(1)),
                         branches=[],
                         parent=None,
                         commitid=None,
                         mergepoint=None,
                         branchpoints=set())

            state = 6

        elif state == 6:
            # expecting date, author, state, lines changed
            match = re_60.match(line)
            assert match, _('revision must be followed by date line')
            d = match.group(1)
            if d[2] == '/':
                # Y2K
                d = '19' + d

            if len(d.split()) != 3:
                # cvs log dates always in GMT
                d = d + ' UTC'
            e.date = parsedate(d, ['%y/%m/%d %H:%M:%S',
                                   '%Y/%m/%d %H:%M:%S',
                                   '%Y-%m-%d %H:%M:%S'])
            e.author = scache(match.group(2))
            e.dead = match.group(3).lower() == 'dead'

            if match.group(5):
                if match.group(6):
                    e.lines = (int(match.group(5)), int(match.group(6)))
                else:
                    e.lines = (int(match.group(5)), 0)
            elif match.group(6):
                e.lines = (0, int(match.group(6)))
            else:
                e.lines = None

            if match.group(7): # cvs 1.12 commitid
                e.commitid = match.group(8)

            if match.group(9): # cvsnt mergepoint
                myrev = match.group(10).split('.')
                if len(myrev) == 2: # head
                    e.mergepoint = 'HEAD'
                else:
                    myrev = '.'.join(myrev[:-2] + ['0', myrev[-2]])
                    branches = [b for b in branchmap if branchmap[b] == myrev]
                    assert len(branches) == 1, ('unknown branch: %s'
                                                % e.mergepoint)
                    e.mergepoint = branches[0]

            e.comment = []
            state = 7

        elif state == 7:
            # read the revision numbers of branches that start at this revision
            # or store the commit log message otherwise
            m = re_70.match(line)
            if m:
                e.branches = [
                    parse_revision(x.strip()) for x in m.group(1).split(';')
                ]
                state = 8
            elif re_31.match(line) and re_50.match(peek):
                state = 5
                store = True
            elif re_32.match(line):
                state = 0
                store = True
            else:
                e.comment.append(line)

        elif state == 8:
            # store commit log message
            if re_31.match(line):
                cpeek = peek
                if cpeek.endswith('\n'):
                    cpeek = cpeek[:-1]
                if re_50.match(cpeek):
                    state = 5
                    store = True
                else:
                    e.comment.append(line)
            elif re_32.match(line):
                state = 0
                store = True
            else:
                e.comment.append(line)

        # When a file is added on a branch B1, CVS creates a synthetic
        # dead trunk revision 1.1 so that the branch has a root.
        # Likewise, if you merge such a file to a later branch B2 (one
        # that already existed when the file was added on B1), CVS
        # creates a synthetic dead revision 1.1.x.1 on B2.  Don't drop
        # these revisions now, but mark them synthetic so
        # createchangeset() can take care of them.
        if (store and
              e.dead and
              e.revision[-1] == 1 and      # 1.1 or 1.1.x.1
              len(e.comment) == 1 and
              file_added_re.match(e.comment[0])):
            ui.debug('found synthetic revision in %s: %r\n'
                     % (e.rcs, e.comment[0]))
            e.synthetic = True

        if store:
            # clean up the results and save in the log.
            store = False
            e.tags = sorted([scache(x) for x in tags.get(e.revision, [])])
            e.comment = scache('\n'.join(e.comment))

            revn = len(e.revision)
            if revn > 3 and (revn % 2) == 0:
                e.branch = tags.get(e.revision[:-1], [None])[0]
            else:
                e.branch = None

            # find the branches starting from this revision
            branchpoints = set()
            for branch, revision in branchmap.items():
                revparts = parse_revision(revision)
                if len(revparts) < 2: # bad tags
                    continue
                if revparts[-2] == 0 and revparts[-1] % 2 == 0:
                    # normal branch
                    if revparts[:-2] == e.revision:
                        branchpoints.add(branch)
                elif revparts == (1, 1, 1): # vendor branch
                    if revparts in e.branches:
                        branchpoints.add(branch)
            e.branchpoints = branchpoints

            log.append(e)

            rcsmap[e.file] = e.rcs

            if len(log) % 100 == 0:
                ui.status(ellipsis('%d %s' % (len(log), e.file), 80) + '\n')

    log.sort(key=lambda x: (x.rcs, x.revision))

    # find parent revisions of individual files
    versions = {}
    for e in sorted(oldlog, key=lambda x: (x.rcs, x.revision)):
        if e.file in rcsmap:
            e.rcs = rcsmap[e.file]
        branch = e.revision[:-1]
        versions[(e.rcs, branch)] = e.revision

    for e in log:
        branch = e.revision[:-1]
        p = versions.get((e.rcs, branch), None)
        if p is None:
            p = e.revision[:-2]
        e.parent = p
        versions[(e.rcs, branch)] = e.revision

    # update the log cache
    if cache:
        if log:
            # join up the old and new logs
            log.sort(key=lambda x: x.date)

            if oldlog and oldlog[-1].date >= log[0].date:
                raise logerror(_('log cache overlaps with new log entries,'
                                 ' re-run without cache.'))

            log = oldlog + log

            # write the new cachefile
            ui.note(_('writing cvs log cache %s\n') % cachefile)
            pickle.dump(log, open(cachefile, 'wb'))
        else:
            log = oldlog

    ui.status(_('%d log entries\n') % len(log))

    # hook.hook(ui, None, "cvslog", True, log=log)

    return log


class changeset(object):
    '''Class changeset has the following attributes:
        .id        - integer identifying this changeset (list index)
        .author    - author name as CVS knows it
        .branch    - name of branch this changeset is on, or None
        .comment   - commit message
        .commitid  - CVS commitid or None
        .date      - the commit date as a (time,tz) tuple
        .entries   - list of logentry objects in this changeset
        .parents   - list of one or two parent changesets
        .tags      - list of tags on this changeset
        .synthetic - from synthetic revision "file ... added on branch ..."
        .mergepoint- the branch that has been merged from or None
        .branchpoints- the branches that start at the current entry or empty
    '''
    def __init__(
        self, author, branch, comment, date, commitid, branchpoints, mergepoint
    ):
        self.id = None
        self.entries = []
        self.parents = []
        self.tags = []
        self.synthetic = False
        self._files = set()
        self._versions = set()
        self.author = author
        self.branch = branch
        self.comment = comment
        self.date = date
        self.commitid = commitid
        self.branchpoints = branchpoints
        self.mergepoint = mergepoint

    @classmethod
    def from_logentry(cls, entry):
        cs = cls(
            entry.author,
            entry.branch,
            entry.comment,
            entry.date,
            entry.commitid,
            entry.branchpoints,
            entry.mergepoint,
        )
        cs._add(entry)
        return cs

    @classmethod
    def from_merge(cls, from_cs, to_cs):
        comment = 'convert-repo: CVS merge from branch %s'
        cs = cls(
            from_cs.author,
            to_cs.branch,
            comment % from_cs.branch,
            from_cs.date,
            None,
            None,
            None,
        )
        cs.parents.append(from_cs)
        cs.parents.append(to_cs)
        return cs

    def _add(self, entry):
        # Synthetic revisions always get their own changeset, because
        # the log message includes the filename.  E.g. if you add file3
        # and file4 on a branch, you get four log entries and three
        # changesets:
        #   "File file3 was added on branch ..." (synthetic, 1 entry)
        #   "File file4 was added on branch ..." (synthetic, 1 entry)
        #   "Add file3 and file4 to fix ..."     (real, 2 entries)
        self.synthetic = not self.entries and entry.synthetic
        self.entries.append(entry)
        # changeset date is date of latest commit in it
        self.date = entry.date
        self._files.add(entry.file)
        self._versions.add((entry.rcs, entry.revision))

    def _can_cover(self, entry, fuzz):
        # Since CVS is file-centric, two different file revisions with
        # different branchpoints should be treated as belonging to two
        # different changesets (and the ordering is important and not
        # honoured by cvsps at this point).
        #
        # Consider the following case:
        # foo 1.1 branchpoints: [MYBRANCH]
        # bar 1.1 branchpoints: [MYBRANCH, MYBRANCH2]
        #
        # Here foo is part only of MYBRANCH, but not MYBRANCH2, e.g. a
        # later version of foo may be in MYBRANCH2, so foo should be the
        # first changeset and bar the next and MYBRANCH and MYBRANCH2
        # should both start off of the bar changeset. No provisions are
        # made to ensure that this is, in fact, what happens.
        if entry.branchpoints != self.branchpoints:
            return False
        if self.commitid is not None:
            return entry.commitid == self.commitid
        else:
            return (
                entry.commitid is None
                and entry.author == self.author
                and entry.branch == self.branch
                and entry.comment == self.comment
                and entry.file not in self._files
                and (
                    (self.date[0] + self.date[1])
                    <= (entry.date[0] + entry.date[1])
                    < (self.date[0] + self.date[1]) + fuzz
                )
            )

    def add_entry(self, entry, fuzz):
        if not self._can_cover(entry, fuzz):
            return False
        self._add(entry)
        return True

    def is_child(self, other):
        for entry in self.entries:
            if (entry.rcs, entry.parent) in other._versions:
                return True
        return False

    def __repr__(self):
        items = ("%s=%r"%(k, self.__dict__[k]) for k in sorted(self.__dict__))
        return "%s(%s)"%(type(self).__name__, ", ".join(items))

def createchangeset(ui, log, fuzz=60, mergefrom=None, mergeto=None):
    '''Convert log into changesets.'''

    ui.status(_('creating changesets\n'))

    # try to order commitids by date
    mindate = {}
    for e in log:
        if e.commitid:
            if e.commitid not in mindate:
                mindate[e.commitid] = e.date
            else:
                mindate[e.commitid] = min(e.date, mindate[e.commitid])

    # Merge changesets
    log.sort(
        key=lambda x: (
            mindate.get(x.commitid, (-1, 0)),
            x.commitid or '',
            x.comment,
            x.author,
            x.branch or '',
            x.date,
            x.branchpoints,
        )
    )

    changesets = []
    c = None
    for i, e in enumerate(log):
        if not (c and c.add_entry(e, fuzz)):
            c = changeset.from_logentry(e)
            changesets.append(c)

            if len(changesets) % 100 == 0:
                t = '%d %s' % (len(changesets), repr(e.comment)[1:-1])
                ui.status(ellipsis(t, 80) + '\n')

    # Sort files in each changeset
    for c in changesets:
        c.entries.sort(key=lambda x: tuple(enumerate(os.path.split(x.file))))

    # Sort changesets by date

    odd = set()

    def cscmp(l, r):
        d = sum(l.date) - sum(r.date)
        if d:
            return d

        # detect vendor branches and initial commits on a branch
        if l.is_child(r):
            d = 1

        if r.is_child(l):
            if d:
                odd.add((l, r))
            d = -1
        # By this point, the changesets are sufficiently compared that
        # we don't really care about ordering. However, this leaves
        # some race conditions in the tests, so we compare on the
        # number of files modified, the files contained in each
        # changeset, and the branchpoints in the change to ensure test
        # output remains stable.

        # recommended replacement for cmp from
        # https://docs.python.org/3.0/whatsnew/3.0.html
        def c(x, y):
            return (x > y) - (x < y)

        # Sort bigger changes first.
        if not d:
            d = c(len(l.entries), len(r.entries))
        # Try sorting by filename in the change.
        if not d:
            d = c([e.file for e in l.entries], [e.file for e in r.entries])
        # Try and put changes without a branch point before ones with
        # a branch point.
        if not d:
            d = c(len(l.branchpoints), len(r.branchpoints))
        return d

    changesets.sort(key=functools.cmp_to_key(cscmp))

    # Collect tags

    globaltags = {}
    for c in changesets:
        for e in c.entries:
            for tag in e.tags:
                # remember which is the latest changeset to have this tag
                globaltags[tag] = c

    for c in changesets:
        tags = set()
        for e in c.entries:
            tags.update(e.tags)
        # remember tags only if this is the latest changeset to have it
        c.tags = sorted(tag for tag in tags if globaltags[tag] is c)

    # Find parent changesets, handle {{mergetobranch BRANCHNAME}}
    # by inserting dummy changesets with two parents, and handle
    # {{mergefrombranch BRANCHNAME}} by setting two parents.

    if mergeto is None:
        mergeto = r'{{mergetobranch ([-\w]+)}}'
    if mergeto:
        mergeto = re.compile(mergeto)

    if mergefrom is None:
        mergefrom = r'{{mergefrombranch ([-\w]+)}}'
    if mergefrom:
        mergefrom = re.compile(mergefrom)

    branches = {}    # changeset index where we saw a branch
    n = len(changesets)
    i = 0
    while i < n:
        c = changesets[i]

        p = None
        if c.branch in branches:
            p = branches[c.branch]
        else:
            # first changeset on a new branch
            # the parent is a changeset with the branch in its
            # branchpoints such that it is the latest possible
            # commit without any intervening, unrelated commits.

            for candidate in range(i):
                if c.branch not in changesets[candidate].branchpoints:
                    if p is not None:
                        break
                    continue
                p = candidate

        if p is not None:
            p = changesets[p]

            # Ensure no changeset has a synthetic changeset as a parent.
            while p.synthetic:
                assert len(p.parents) <= 1, \
                       _('synthetic changeset cannot have multiple parents')
                if p.parents:
                    p = p.parents[0]
                else:
                    p = None
                    break

            if p is not None:
                c.parents.append(p)

        if c.mergepoint:
            if c.mergepoint == 'HEAD':
                c.mergepoint = None
            c.parents.append(changesets[branches[c.mergepoint]])

        if mergefrom:
            m = mergefrom.search(c.comment)
            if m:
                m = m.group(1)
                if m == 'HEAD':
                    m = None
                try:
                    candidate = changesets[branches[m]]
                except KeyError:
                    ui.warn(_("warning: CVS commit message references "
                              "non-existent branch %r:\n%s\n")
                            % (m, c.comment))
                if m in branches and c.branch != m and not candidate.synthetic:
                    c.parents.append(candidate)

        if mergeto:
            m = mergeto.search(c.comment)
            if m:
                if m.groups():
                    m = m.group(1)
                    if m == 'HEAD':
                        m = None
                else:
                    m = None   # if no group found then merge to HEAD
                if m in branches and c.branch != m:
                    # insert empty changeset for merge
                    cc = changeset.from_merge(c, changesets[branches[m]])
                    changesets.insert(i + 1, cc)
                    branches[m] = i + 1

                    # adjust our loop counters now we have inserted a new entry
                    n += 1
                    i += 2
                    continue

        branches[c.branch] = i
        i += 1

    # Drop synthetic changesets (safe now that we have ensured no other
    # changesets can have them as parents).
    i = 0
    while i < len(changesets):
        if changesets[i].synthetic:
            del changesets[i]
        else:
            i += 1

    # Number changesets

    for i, c in enumerate(changesets):
        c.id = i + 1

    if odd:
        for l, r in odd:
            if l.id is not None and r.id is not None:
                ui.warn(_('changeset %d is both before and after %d\n')
                        % (l.id, r.id))

    ui.status(_('%d changeset entries\n') % len(changesets))

    # hook.hook(ui, None, "cvschangesets", True, changesets=changesets)

    return changesets


def debugcvsps(ui, *args, **opts):
    '''Read CVS rlog for current directory or named path in
    repository, and convert the log to changesets based on matching
    commit log entries and dates.
    '''
    if opts["new_cache"]:
        cache = "write"
    elif opts["update_cache"]:
        cache = "update"
    else:
        cache = "read"

    revisions = opts["revisions"]

    try:
        if args:
            log = []
            for d in args:
                log += createlog(ui, d, root=opts["root"], cache=cache)
        else:
            log = createlog(ui, root=opts["root"], cache=cache)
    except logerror as e:
        ui.write("%r\n"%e)
        return

    changesets = createchangeset(ui, log, opts["fuzz"])
    del log

    # Print changesets (optionally filtered)

    off = len(revisions)
    branches = {}    # latest version number in each branch
    ancestors = {}   # parent branch
    for cs in changesets:

        if opts["ancestors"]:
            if cs.branch not in branches and cs.parents and cs.parents[0].id:
                ancestors[cs.branch] = (changesets[cs.parents[0].id - 1].branch,
                                        cs.parents[0].id)
            branches[cs.branch] = cs.id

        # limit by branches
        if opts["branches"] and (cs.branch or 'HEAD') not in opts["branches"]:
            continue

        if not off:
            cs_date = min([e.date for e in cs.entries])
            cs_tags = cs.tags[:1]
            # Note: trailing spaces on several lines here are needed to have
            #       bug-for-bug compatibility with cvsps.
            ui.write('---------------------\n')
            ui.write(('PatchSet %d \n' % cs.id))
            ui.write(('Date: %s\n' % datestr(cs_date,
                                             '%Y/%m/%d %H:%M:%S %1%2')))
            ui.write(('Author: %s\n' % cs.author))
            ui.write(('Branch: %s\n' % (cs.branch or 'HEAD')))
            ui.write(('Tag%s: %s \n' % (['', 's'][len(cs_tags) > 1],
                                        ','.join(cs_tags) or '(none)')))
            if cs.branchpoints:
                ui.write(('Branchpoints: %s \n') %
                         ', '.join(sorted(cs.branchpoints)))
            if opts["parents"] and cs.parents:
                if len(cs.parents) > 1:
                    ui.write(('Parents: %s\n' %
                             (','.join([str(p.id) for p in cs.parents]))))
                else:
                    ui.write(('Parent: %d\n' % cs.parents[0].id))

            if opts["ancestors"]:
                b = cs.branch
                r = []
                while b:
                    b, c = ancestors[b]
                    r.append('%s:%d:%d' % (b or "HEAD", c, branches[b]))
                if r:
                    ui.write(('Ancestors: %s\n' % (','.join(r))))

            ui.write(('Log:\n'))
            ui.write('%s\n\n' % cs.comment)
            ui.write(('Members: \n'))
            for f in cs.entries:
                fn = f.file
                if fn.startswith(opts["prefix"]):
                    fn = fn[len(opts["prefix"]):]
                ui.write('\t%s:%s->%s%s \n' % (
                        fn, '.'.join([str(x) for x in f.parent]) or 'INITIAL',
                        '.'.join([str(x) for x in f.revision]),
                        ['', '(DEAD)'][f.dead]))
            ui.write('\n')

        # have we seen the start tag?
        if revisions and off:
            if revisions[0] == str(cs.id) or \
                revisions[0] in cs.tags:
                off = False

        # see if we reached the end tag
        if len(revisions) > 1 and not off:
            if revisions[1] == str(cs.id) or \
                revisions[1] in cs.tags:
                break

def main():
    '''Main program to mimic cvsps.'''

    op = OptionParser(
        usage='%prog [-bpruvxz] path',
        description='Read CVS rlog for current directory or named '
        'path in repository, and convert the log to changesets '
        'based on matching commit log entries and dates.',
    )

    # Options that are ignored for compatibility with cvsps-2.1
    op.add_option('-A', dest='ignore', action='store_true', help=SUPPRESS_HELP)
    op.add_option(
        '--cvs-direct', dest='ignore', action='store_true', help=SUPPRESS_HELP
    )
    op.add_option('-q', dest='ignore', action='store_true', help=SUPPRESS_HELP)
    op.add_option(
        '--norc', dest='ignore', action='store_true', help=SUPPRESS_HELP
    )

    # Main options shared with cvsps-2.1
    op.add_option(
        '-b',
        dest='branches',
        action='append',
        default=[],
        help='Only return changes on specified branches',
    )
    op.add_option(
        '-p',
        dest='prefix',
        action='store',
        default='',
        help='Prefix to remove from file names',
    )
    op.add_option(
        '-r',
        dest='revisions',
        action='append',
        default=[],
        help='Only return changes after or between specified tags',
    )
    op.add_option(
        '-u',
        dest='update_cache',
        action='store_true',
        help="Update cvs log cache",
    )
    op.add_option(
        '-v', dest='verbose', action='count', default=0, help='Be verbose'
    )
    op.add_option(
        '-x',
        dest='new_cache',
        action='store_true',
        help="Create new cvs log cache",
    )
    op.add_option(
        '-z',
        dest='fuzz',
        action='store',
        type='int',
        default=60,
        help='Set commit time fuzz',
        metavar='seconds',
    )
    op.add_option(
        '--root',
        dest='root',
        action='store',
        default='',
        help='Specify cvsroot',
        metavar='cvsroot',
    )

    # Options specific to this version
    op.add_option(
        '--parents',
        dest='parents',
        action='store_true',
        help='Show parent changesets',
    )
    op.add_option(
        '--ancestors',
        dest='ancestors',
        action='store_true',
        help='Show current changeset in ancestor branches',
    )

    options, args = op.parse_args()

    opts = dict()
    for attr in vars(options):
        if attr not in ('ignore', 'verbose'):
            opts[attr] = getattr(options, attr)

    # Create a ui object for printing progress messages
    class UI:
        def __init__(self, verbose):
            if verbose:
                self.status = self.message
            if verbose > 1:
                self.note = self.message
            if verbose > 2:
                self.debug = self.message

        def write(self, msg):
            sys.stdout.buffer.write(msg.encode('latin-1'))

        def message(self, msg):
            sys.stderr.buffer.write(msg.encode('latin-1'))

        def nomessage(self, msg):
            pass

        status = nomessage
        note = nomessage
        debug = nomessage

    ui = UI(options.verbose)

    debugcvsps(ui, *args, **opts)

if __name__ == '__main__':
    main()
