#!/usr/bin/env python3

# ----------------------------------------------------------------------------
# Copyright (c) 2013--, Qiyun Zhu and Katharina Dittmar.
#
# Distributed under the terms of the Modified BSD License.
#
# The full license is in the file LICENSE, distributed with this software.
# ----------------------------------------------------------------------------

import re
from os import remove, makedirs, cpu_count
from os.path import join, isdir, isfile
from shutil import which, rmtree
from tempfile import mkdtemp
from time import time, sleep
from math import log
from urllib.parse import quote
from urllib.request import urlopen, HTTPError, URLError

from hgtector.util import (
    timestamp, load_configs, get_config, arg2bool, list_from_param, file2id,
    id2file_map, read_taxdump, read_input_prots, read_prot2taxid,
    get_product, seqid2accver, write_fasta, run_command, contain_words,
    is_latin, is_capital, is_ancestral, taxid_at_rank)


description = """batch sequence homology searching and filtering"""

arguments = [
    'basic',
    ['-i|--input',   'input protein set file, or directory where one or more '
                     'input files are located', {'required': True}],
    ['-o|--output',  'directory where search results are to be saved',
                     {'required': True}],
    ['-m|--method',  'search method',
                     {'choices': ['auto', 'diamond', 'blast', 'remote',
                      'precomp'], 'default': 'auto'}],
    ['-s|--precomp', 'file or directory of precomputed search results (when '
                     'method = precomp)'],

    'database',
    ['-d|--db',      'reference protein sequence database'],
    ['-t|--taxdump', 'directory of taxonomy database files (nodes.dmp and '
                     'names.dmp)'],
    ['--taxmap',     'sequence Id to taxId mapping file (not necessary if '
                     'protein database already contains taxonomy)'],

    'search behaviors',
    ['-k|--maxhits', 'maximum number of hits to preserve per query (0 for '
                     'unlimited)', {'type': int}],
    ['--minsize',    'minimum length of query sequence (aa)', {'type': int}],
    ['--queries',    'number of queries per run (0 for whole sample)',
                     {'type': int}],
    ['--maxchars',   'maximum number of characters per run (0 for unlimited)',
                     {'type': int}],

    'search cutoffs',
    ['--maxseqs',    'maximum number of sequences to return', {'type': int}],
    ['--evalue',     'maximum E-value cutoff', {'type': float}],
    ['--identity',   'minimum percent identity cutoff', {'type': int}],
    ['--coverage',   'minimum percent query coverage cutoff', {'type': int}],
    ['--extrargs',   'extra arguments for choice of search method'],

    'taxonomic filters',
    ['--tax-include', 'include taxa under those taxIds'],
    ['--tax-exclude', 'exclude taxa under those taxIds'],
    ['--tax-unique',  'ignore more than one hit with same taxId',
                      {'choices': ['yes', 'no']}],
    ['--tax-unirank', 'ignore more than one hit under same taxon at this '
                      'rank'],
    ['--tax-capital', 'ignore taxon names that are not capitalized',
                      {'choices': ['yes', 'no']}],
    ['--tax-latin',   'ignore species names that are not Latinate',
                      {'choices': ['yes', 'no']}],
    ['--tax-block',   'ignore taxon names containing those words'],

    'local search behaviors',
    ['-p|--threads', 'number of threads (0 for all CPU cores)',
                     {'type': int}],
    ['--tmpdir',     'temporary directory'],
    ['--diamond',    'diamond executable'],
    ['--blastp',     'blastp executable'],
    ['--blastdbcmd', 'blastdbcmd executable'],

    'remote search behaviors',
    ['--algorithm',  'remote search algorithm'],
    ['--retries',    'maximum number of retries per search',
                     {'type': int}],
    ['--delay',      'seconds between two search requests',
                     {'type': int}],
    ['--timeout',    'seconds before program gives up waiting',
                     {'type': int}],
    ['--entrez',     'entrez query text'],
    ['--server',     'remote search server URL'],

    'self-alignment options',
    ['--aln-method',  'self-alignment method',
                      {'choices': ['auto', 'native', 'fast', 'lookup',
                       'precomp']}],
    ['--aln-precomp', 'file or directory of precomputed sequence Id to score '
                      'maps (when self-alignment method = precomp)'],
    ['--aln-server',  'remote align server URL'],

    'remote fetch options',
    ['--fetch-enable',  'whether to enable remote fetch',
                        {'choices': ['auto', 'yes', 'no']}],
    ['--fetch-queries', 'maximum number of query entries per search'],
    ['--fetch-retries', 'maximum number of retries per search'],
    ['--fetch-delay',   'seconds between two fetch requests',
                        {'type': int}],
    ['--fetch-timeout', 'seconds before program gives up waiting',
                        {'type': int}],
    ['--fetch-server',  'remote fetch server URL'],
]


class Search(object):

    def __init__(self):
        self.arguments = arguments
        self.description = description

    def __call__(self, args):
        print('Homology search started at {}.'.format(timestamp()))

        # load configurations
        self.cfg = load_configs()

        # read and validate arguments
        self.args_wf(args)

        # read and validate input data
        self.input_wf()

        # perform homology search for each sample
        for sid, sample in sorted(self.data.items()):
            if 'done' in sample:
                continue
            prots = sample['prots']

            print('Batch homology search of {} started at {}.'.format(
                sid, timestamp()))

            # collect sequences to search
            id2idx = {}
            seqs = []
            for i, prot in enumerate(prots):
                if 'hits' in prot:
                    continue
                id_ = prot['id']
                seqs.append((id_, prot['seq']))
                id2idx[id_] = i
            print('Number of queries: {}.'.format(len(seqs)))

            # divide sequences into batches
            batches = ([seqs] if self.method == 'precomp' else
                       self.subset_seqs(seqs, self.queries, self.maxchars))

            # run search for each batch
            n = 0
            for batch in batches:

                # batch homology search
                res = self.search_wf(
                    batch, self.pcmap[sid] if self.method == 'precomp'
                    else None)

                # update taxIds of hits
                self.taxid_wf(res)

                # update taxonomic information of new taxIds
                self.taxinfo_wf(res)

                # perform taxonomy-based filtering
                self.taxfilt_wf(res)

                # update samples with search results
                indices = [id2idx[x[0]] for x in batch]
                self.update_search_results(prots, res, set(indices))

                # perform self-alignment
                seqs2a = [x for x in batch if 'score' not in prots[
                    id2idx[x[0]]]]
                if seqs2a:
                    for id_, score in self.selfaln_wf(seqs2a, res).items():
                        prots[id2idx[id_]]['score'] = score

                # write search results to file
                with open(join(self.output, '{}.tsv'.format(sid)), 'a') as f:
                    self.write_search_results(f, prots, indices)

                n += len(batch)
                print('  {} queries completed.'.format(n))

            print('Batch homology search of {} ({} proteins) ended at {}.'
                  .format(sid, len(prots), timestamp()))

        # clean up
        if hasattr(self, 'mkdtemp'):
            rmtree(self.tmpdir)

        print('Batch homology search finished at {}.'.format(timestamp()))

    """master workflows"""

    def args_wf(self, args):
        """Workflow for validating and setting arguments.

        Parameters
        ----------
        args : dict
            command-line arguments

        Notes
        -----
        Workflow:
        1. Load command-line arguments.
        2. Update arguments from configurations.
        3. Validate input and output directories.
        4. Determine search method and parameters.
        5. Determine taxonomy database and map.
        6. Determine self-alignment method and parameters.
        7. Determine remote fetch method and parameters.
        8. Print major settings if desired.
        """
        # load arguments
        for key, val in vars(args).items():
            setattr(self, key, val)

        # check input directory and data
        if isfile(self.input):
            self.input_map = {file2id(self.input): self.input}
        elif isdir(self.input):
            self.input_map = {k: join(self.input, v) for k, v in id2file_map(
                self.input).items()}
            if len(self.input_map) == 0:
                raise ValueError('No input data are found under: {}.'
                                 .format(self.input))
        else:
            raise ValueError('Invalid input data file or directory: {}.'
                             .format(self.input))

        # check / create output directory
        makedirs(self.output, exist_ok=True)
        self.prev_map = id2file_map(self.output, 'tsv')

        """determine search strategy"""

        # load search parameters
        get_config(self, 'evalue', 'search.evalue', float)
        for key in ('method', 'minsize', 'maxseqs', 'identity', 'coverage'):
            get_config(self, key, 'search.{}'.format(key))
        for key in ('diamond', 'blastp', 'blastdbcmd'):
            get_config(self, key, 'program.{}'.format(key))

        if self.method not in {'auto', 'diamond', 'blast', 'remote',
                               'precomp'}:
            raise ValueError('Invalid search method: {}.'.format(self.method))

        # look for precomputed search results
        if self.method == 'precomp' and not self.precomp:
            raise ValueError('Must specify location of pre-computed search '
                             'results.')
        if self.precomp:
            if isfile(self.precomp):
                if len(self.input_map) > 1:
                    raise ValueError('A directory of multiple pre-computed '
                                     'search result files is needed.')
                self.pcmap = {file2id(self.precomp): self.precomp}
            elif isdir(self.precomp):
                self.pcmap = {k: join(self.precomp, v) for k, v in id2file_map(
                    self.precomp).items()}
                if len(self.pcmap) == 0:
                    raise ValueError('Cannot locate any pre-computed search '
                                     'results at: {}.'.format(self.precomp))
            else:
                raise ValueError('Invalid pre-computed search result file or '
                                 'directory: {}.'.format(self.precomp))
            if self.method == 'auto':
                self.method = 'precomp'

        # check local search executables and databases
        diamond_db = self.check_diamond(self.cfg)
        blast_db = self.check_blast(self.cfg)

        # choose a local search method if available, or do remote search
        if self.method == 'auto':
            if diamond_db:
                self.method = 'diamond'
                self.db = diamond_db
            elif blast_db:
                self.method = 'blast'
                self.db = blast_db
            else:
                self.method = 'remote'

        # load method-specific arguments
        for key in ('queries', 'maxchars', 'extrargs'):
            get_config(self, key, '{}.{}'.format(self.method, key))

        # load remote search settings
        if self.method == 'remote':
            for key in ('db', 'algorithm', 'delay', 'timeout', 'entrez'):
                get_config(self, 'db', 'remote.{}'.format(key))
            get_config(self, 'server', 'server.search')

        # determine number of threads
        if self.method in ('diamond', 'blast') and not self.threads:

            # use all available CPUs if threads is set to zero or left empty
            self.threads = cpu_count()

            # do single-threading if CPU count not working
            if self.threads is None:
                print('WARNING: Cannot determine number of CPUs. Will do '
                      'single-threading instead.')
                self.threads = 1

            # apply BLAST CPU number cap
            if self.method == 'blast' and self.threads > 8:
                print('WARNING: BLAST can only use a maximum of 8 CPUs.')
                self.threads = 8

        # check / create temporary directory
        if self.method in ('diamond', 'blast'):
            if not self.tmpdir:
                self.tmpdir = mkdtemp()
                setattr(self, 'mkdtemp', True)  # mark for cleanup
            if not isdir(self.tmpdir):
                raise ValueError('Invalid temporary directory: {}.'.format(
                    self.tmpdir))

        """determine taxonomy database and filters"""

        # initialize protein-to-taxId map
        self.prot2tid = {}

        # assign taxonomy database
        for key in ('taxdump', 'taxmap'):
            get_config(self, key, 'database.{}'.format(key))

        if self.method != 'remote':

            # check local taxonomy database
            if not self.taxdump:
                print('WARNING: Local taxonomy database is not specified. '
                      'Will try to retrieve taxonomic information from remote '
                      'server.')
            elif not isdir(self.taxdump):
                raise ValueError('Invalid taxonomy database directory: {}.'
                                 .format(self.taxdump))
            else:
                for fname in ('names.dmp', 'nodes.dmp'):
                    if not isfile(join(self.taxdump, fname)):
                        raise ValueError('Taxonomy database file {} is not '
                                         'found.'.format(fname))

            # check local taxonomy map
            if self.taxmap and not isfile(self.taxmap):
                raise ValueError('Invalid protein-to-taxId map: {}.'.format(
                    self.taxmap))

        # load taxonomic filters and convert to lists
        for key in ('include', 'exclude', 'block'):
            attr = 'tax_{}'.format(key)
            get_config(self, attr, 'taxonomy.{}'.format(key))
            setattr(self, attr, list_from_param(getattr(self, attr)))

        # load taxonomy switches
        for key in ('unique', 'unirank', 'capital', 'latin'):
            get_config(self, 'tax_{}'.format(key), 'taxonomy.{}'.format(key))

        """determine self-alignment strategy"""

        # load configurations
        get_config(self, 'aln_method', 'search.selfaln')
        get_config(self, 'aln_server', 'server.selfaln')
        if self.aln_method not in {'auto', 'native', 'fast', 'lookup',
                                   'precomp'}:
            raise ValueError('Invalid self-alignment method: {}.'.format(
                self.aln_method))

        # look for precomputed self-alignment results
        if self.aln_method == 'precomp' and not self.aln_precomp:
            raise ValueError('Must specify location of pre-computed self-'
                             'alignment scores.')
        if self.aln_precomp:
            if isfile(self.aln_precomp):
                if len(self.input_map) > 1:
                    raise ValueError('A directory of multiple pre-computed '
                                     'self-alignment result files is needed.')
                self.aln_pcmap = {file2id(self.aln_precomp): self.aln_precomp}
            elif isdir(self.aln_precomp):
                self.aln_pcmap = {k: join(self.aln_precomp, v) for k, v in
                                  id2file_map(self.aln_precomp).items()}
                if len(self.aln_pcmap) == 0:
                    raise ValueError('Cannot locate any pre-computed self-'
                                     'alignment results at: {}.'.format(
                                         self.aln_precomp))
            else:
                raise ValueError('Invalid pre-computed self-alignment result '
                                 'file or directory: {}.'.format(
                                     self.aln_precomp))
            if self.aln_method == 'auto':
                self.aln_method = 'precomp'

        # use the same search method for self-alignment, otherwise use fast
        if self.aln_method in ('auto', 'native'):
            self.aln_method = 'fast' if self.method == 'precomp' else 'native'

        """determine fetch strategy"""

        # load configurations
        get_config(self, 'fetch_server', 'server.fetch')
        for key in ('enable', 'queries', 'retries', 'delay', 'timeout'):
            get_config(self, 'fetch_{}'.format(key), 'fetch.{}'.format(key))

        # determine remote or local fetching
        if self.fetch_enable == 'auto':
            self.fetch_enable = 'yes' if (
                self.method == 'remote' or not self.taxdump) else 'no'

        """final steps"""

        # convert boolean values
        for key in ('tax_unique', 'tax_capital', 'tax_latin'):
            setattr(self, key, arg2bool(getattr(self, key, None)))

        # convert fractions to percentages
        for metric in ('identity', 'coverage'):
            val = getattr(self, metric)
            if val and val < 1:
                setattr(self, metric, val * 100)

        # print major settings
        print('Settings:')
        print('  Search method: {}.'.format(self.method))
        print('  Self-alignment method: {}.'.format(self.aln_method))
        print('  Remote fetch enabled: {}.'.format(self.fetch_enable))

    def check_diamond(self):
        """Check if DIAMOND is available.

        Returns
        -------
        str or None
            valid path to DIAMOND database, or None if not available

        Raises
        ------
        ValueError
            if settings conflict
        """
        if self.method in ('diamond', 'auto'):
            if not self.diamond:
                self.diamond = 'diamond'
            if which(self.diamond):
                try:
                    db_ = self.db or self.cfg['database']['diamond']
                except KeyError:
                    pass
                if db_:
                    if isfile(db_) or isfile('{}.dmnd'.format(db_)):
                        return db_
                    elif self.method == 'diamond':
                        raise ValueError('Invalid DIAMOND database: {}.'
                                         .format(db_))
                elif self.method == 'diamond':
                    raise ValueError('A protein database is required for '
                                     'DIAMOND search.')
            elif self.method == 'diamond':
                raise ValueError('Invalid diamond executable: {}.'.format(
                    self.diamond))
        return None

    def check_blast(self):
        """Check if BLAST is available.

        Returns
        -------
        str or None
            valid path to BLAST database, or None if not available

        Raises
        ------
        ValueError
            if settings conflict
        """
        if self.method in ('blast', 'auto'):
            if not self.blastp:
                self.blastp = 'blastp'
            if which(self.blastp):
                try:
                    db_ = self.db or self.cfg['database']['blast']
                except KeyError:
                    pass
                if db_:
                    found = True
                    for ext in ('phr', 'pin', 'psq'):
                        if not isfile('{}.{}'.format(db_, ext)):
                            found = False
                    if found:
                        return db_
                    elif self.method == 'blast':
                        raise ValueError('Invalid BLAST database: {}.'
                                         .format(db_))
                elif self.method == 'blastp':
                    raise ValueError('A protein database is required for '
                                     'BLAST search.')
            elif self.method == 'blastp':
                raise ValueError('Invalid blastp executable: {}.'.format(
                    self.blastp))
        return None

    def input_wf(self):
        """Master workflow for processing input data.

        Notes
        -----
        Workflow:
        1. Read proteins of each protein set.
        2. Process search results from previous runs.
        3. Import precomputed self-alignment scores.
        4. Fetch sequences for proteins to be searched.
        5. Drop sequences shorter than threshold.
        6. Read or initiate taxonomy database.
        7. Read protein-to-taxId map.
        """
        # initiate data structure
        self.data = {}

        # read proteins of each sample
        print('Reading input proteins...')
        nprot = 0
        for id_, fname in self.input_map.items():
            prots = read_input_prots(fname)
            n = len(prots)
            if n == 0:
                raise ValueError('No protein entries are found for {}.'
                                 .format(id_))
            print('  {}: {} proteins.'.format(id_, n))
            self.data[id_] = {'prots': prots}
            nprot += n
        print('Done. Read {} proteins from {} samples.'
              .format(nprot, len(self.data)))

        # process search results from previous runs
        ndone = 0
        if self.prev_map:
            print('Processing search results from previous runs...')
            for id_ in self.data:
                if id_ not in self.prev_map:
                    continue
                prots_ = self.data[id_]['prots']
                n = len(prots_)
                m = len(self.parse_prev_results(join(
                    self.output, self.prev_map[id_]), prots_))
                if m == n:
                    self.data[id_]['done'] = True
                ndone += m
            print('Done. Found results for {} proteins, remaining {} to '
                  'search.'.format(ndone, nprot - ndone))

        # check if search is already completed
        if (ndone == nprot):
            return

        # import precomputed self-alignment scores
        if self.aln_method == 'precomp' and hasattr(self, 'aln_pcmap'):
            n, m = 0, 0
            print('Importing precomputed self-alignment scores...', end='')
            for sid, file in self.aln_pcmap.items():

                # read scores
                scores = {}
                with open(file, 'r') as f:
                    for line in f:
                        line = line.rstrip('\r\n')
                        if not line or line.startswith('#'):
                            continue
                        id_, score = line.split('\t')
                        scores[id_] = float(score)

                # assign scores if not already
                for prot in self.data[sid]['prots']:
                    if 'score' in prot:
                        continue
                    n += 1
                    id_ = prot['id']
                    try:
                        prot['score'] = scores[id_]
                        m += 1
                    except KeyError:
                        pass
            print(' done.')
            print('  Imported scores for {} proteins.'.format(n))
            dif = n - m
            if dif > 0:
                raise ValueError('Missing scores for {} proteins.'.format(dif))

        # fetch sequences for unsearched proteins
        seqs2q = self.check_missing_seqs(self.data)
        n = len(seqs2q)
        if n > 0:
            print('Sequences of {} proteins are to be retrieved.'.format(n))
            if self.method == 'blast':
                print('Fetching sequences from local BLAST database...',
                      end='')
                seqs = self.blast_seqinfo(seqs2q)
                n = self.update_prot_seqs(seqs)
                print(' done.')
                print('  Obtained sequences of {} proteins.'.format(n))
                seqs2q = self.check_missing_seqs(self.data)
                n = len(seqs2q)
                if n > 0:
                    print('  Remaining {} proteins.'.format(n))
            if n > 0 and self.fetch_enable == 'yes':
                print('Fetching {} sequences from remote server...'.format(n),
                      flush=True)
                seqs = self.remote_seqinfo(seqs2q)
                n = self.update_prot_seqs(seqs)
                print('Done. Obtained sequences of {} proteins.'.format(n))
                seqs2q = self.check_missing_seqs(self.data)
                n = len(seqs2q)
            if n > 0:
                raise ValueError(
                    '  Cannot obtain sequences of {} proteins.'.format(n))

        # drop short sequences
        if self.minsize:
            print('Dropping sequences shorter than {} aa...'.format(
                self.minsize), end='')
            for sid, sample in self.data.items():
                for i in reversed(range(len(sample['prots']))):
                    if len(sample['prots'][i]['seq']) < self.minsize:
                        del sample['prots'][i]
            print(' done.')

        # read or initiate taxonomy database
        # read external taxdump
        if self.taxdump is not None:
            print('Reading local taxonomy database...', end='')
            self.taxdump = read_taxdump(self.taxdump)
            print(' done.')
            print('  Read {} taxa.'.format(len(self.taxdump)))

        # read taxdump generated by previous runs
        elif (isfile(join(self.output, 'names.dmp')) and
                isfile(join(self.output, 'nodes.dmp'))):
            print('Reading custom taxonomy database...', end='')
            self.taxdump = read_taxdump(self.output)
            print(' done.')
            print('  Read {} taxa.'.format(len(self.taxdump)))

        # build taxdump from scratch
        else:
            print('Initiating custom taxonomy database...', end='')
            self.taxdump = {'1': {
                'name': 'root', 'parent': '1', 'rank': 'no rank'}}
            self.update_dmp_files(['1'])
            print(' done.')

        # record invalid taxIds to save compute
        self.badtaxids = set()

    def search_wf(self, seqs, file=None):
        """Master workflow for batch homology search.

        Parameters
        ----------
        seqs : list of tuple
            query sequences (Id, sequence)
        file : str, optional
            file of precomputed results

        Returns
        -------
        dict
            sequence Id to hit table

        Notes
        -----
        Workflow:
        1. Generate an Id-to-length map.
        2. Import precomputed search results (if method = precomp).
        3. Call choice of method (remote, blast or diamond) to search.
        """
        # generate an Id-to-length map
        lenmap = {x[0]: len(x[1]) for x in seqs}

        # import pre-computed search results
        if self.method == 'precomp':
            print('Importing pre-computed search results...', end='')
            res = self.parse_hit_table(file, lenmap)
            print(' done.')
            print('  Found hits for {} proteins.'.format(len(res)))

        # run de novo search
        elif self.method == 'remote':
            res = self.remote_search(seqs)
            sleep(self.delay)
        elif self.method == 'blast':
            res = self.blast_search(seqs)
        elif self.method == 'diamond':
            res = self.diamond_search(seqs)

        return res

    def taxid_wf(self, prots):
        """Master workflow for associating hits with taxIds.

        Parameters
        ----------
        prots : dict of list
            proteins (search results)

        Notes
        -----
        Workflow:
        1. Update taxmap with taxIds directly available from hit tables.
        2. Get taxIds for sequence Ids without them:
        2.1. Query external taxon map, if available.
        2.2. Do local fetch (from a BLAST database) if available.
        2.3. Do remote fetch (from NCBI server) if available.
        3. Delete hits without associated taxIds.
        """
        added_taxids = set()

        # update taxon map with taxIds already in hit tables
        ids2q, added = self.update_hit_taxids(prots)
        added_taxids.update(added)

        # attempt to look up taxIds from external taxon map
        if ids2q and self.method != 'remote' and self.taxmap is not None:

            # load taxon map on first use (slow and memory-hungry)
            if isinstance(self.taxmap, str):
                print('Reading protein-to-TaxID map (WARNING: may be slow and '
                      'memory-hungry)...', end='', flush=True)
                self.taxmap = read_prot2taxid(self.taxmap)
                print(' done.')
                print('  Read {} records.'.format(len(self.taxmap)))

            ids2q, added = self.update_hit_taxids(prots, self.taxmap)
            added_taxids.update(added)

        # attempt to look up taxIds from local BLAST database
        if (ids2q and self.method in ('blast', 'precomp') and self.db
                and self.blastdbcmd):
            newmap = {x[0]: x[1] for x in self.blast_seqinfo(ids2q)}
            ids2q, added = self.update_hit_taxids(prots, newmap)
            added_taxids.update(added)

        # attempt to look up taxIds from remote server
        if ids2q and self.fetch_enable == 'yes':
            print('Fetching taxIds of {} sequences from remote server...'
                  .format(len(ids2q)), flush=True)
            newmap = {x[0]: x[1] for x in self.remote_seqinfo(ids2q)}
            print('Done. Obtained taxIds of {} sequences.'.format(len(newmap)))
            ids2q, added = self.update_hit_taxids(prots, newmap)
            added_taxids.update(added)

        # drop hits whose taxIds cannot be identified
        n = len(ids2q)
        if n > 0:
            print('WARNING: Cannot obtain taxIds for {} sequences. These hits '
                  'will be dropped.'.format(n))
            for hits in prots.values():
                for i in reversed(range(len(hits))):
                    if hits[i]['id'] in ids2q:
                        del hits[i]

    def taxinfo_wf(self, prots):
        """Master workflow for associating hits with taxonomic information.

        Parameters
        ----------
        prots : dict of list
            proteins (search results)

        Notes
        -----
        Workflow:
        1. Obtain a list of taxIds represented by hits.
        2. List taxIds that are missing in current taxonomy database.
        3. Get taxonomic information for taxIds by remote fetch, if available.
        4. Append new taxonomic information to dump files, if available.
        5. Delete hits whose taxIds are not associated with information.
        """
        # list taxIds without information
        tids2q = set()
        for prot, hits in prots.items():
            for hit in hits:
                tid = hit['taxid']
                if tid not in self.taxdump:
                    tids2q.add(tid)
        tids2q = sorted(tids2q)

        # retrieve information for these taxIds
        if tids2q and self.fetch_enable == 'yes':
            print('Fetching {} taxIds and ancestors from remote server...'
                  .format(len(tids2q)), flush=True)
            xml = self.remote_taxinfo(tids2q)
            added = self.parse_taxonomy_xml(xml)
            print('Done. Obtained taxonomy of {} taxIds.'.format(len(tids2q)))
            self.update_dmp_files(added)
            tids2q = [x for x in tids2q if x not in added]

        # drop taxIds whose information cannot be obtained
        if tids2q:
            print('WARNING: Cannot obtain information of {} taxIds. These '
                  'hits will be dropped.')
            tids2q = set(tids2q)
            for hits in prots.values():
                for i in reversed(range(len(hits))):
                    if hits[i]['taxid'] in tids2q:
                        del hits[i]
            self.badtaxids.update(tid)

    def taxfilt_wf(self, prots):
        """Workflow for filtering hits by taxonomy.

        Parameters
        ----------
        prots : dict of list of dict
            proteins (search results)

        Notes
        -----
        Workflow:
        1. Bottom-up filtering (delete in place)
        1.1. Delete taxIds already marked as bad.
        1.2. Delete taxIds whose ancestors are not in the "include" list.
        1.3. Delete taxIds, any of whose ancestors is in the "exclude" list.
        1.4. Delete empty taxon names.
        1.5. Delete taxon names that are not capitalized.
        1.6. Delete taxon names in which any word is in the "block" list.
        2. Bottom-up filtering (mark and batch delete afterwards)
        2.1. Mark taxIds that already appeared in hit table for deletion.
        2.2. Mark taxIds whose ancestor at given rank already appeared in hit
            table for deletion.
        """
        # filtering of taxIds and taxon names
        for id_, hits in prots.items():

            # bottom-up filtering by independent criteria
            for i in reversed(range(len(hits))):
                todel = False
                tid = hits[i]['taxid']
                taxon = self.taxdump[tid]['name']
                if tid in self.badtaxids:
                    todel = True
                elif (self.tax_include and not
                      is_ancestral(tid, self.tax_include, self.taxdump)):
                    todel = True
                elif (self.tax_exclude and
                      is_ancestral(tid, self.tax_exclude, self.taxdump)):
                    todel = True
                elif taxon == '':
                    todel = True
                elif self.tax_capital and not is_capital(taxon):
                    todel = True
                elif self.tax_block and contain_words(taxon, self.tax_block):
                    todel = True
                elif self.tax_latin:
                    tid_ = taxid_at_rank(tid, 'species', self.taxdump)
                    if not tid_ or not is_latin(tid_):
                        todel = True
                if todel:
                    del hits[i]
                    self.badtaxids.add(tid)

            # top-down filtering by sorting-based criteria
            todels = []
            used = set()
            used_at_rank = set()
            for i in range(len(hits)):
                tid = hits[i]['taxid']
                if self.tax_unique:
                    if tid in used:
                        todels.append(i)
                        continue
                    else:
                        used.add(tid)
                if self.tax_unirank:
                    tid_ = taxid_at_rank(tid, self.tax_unirank, self.taxdump)
                    if tid_ and tid_ in used_at_rank:
                        todels.append(i)
                    else:
                        used_at_rank.add(tid_)
            for i in reversed(todels):
                del hits[i]

    def selfaln_wf(self, seqs, prots=None):
        """Master workflow for protein sequence self-alignment.

        Parameters
        ----------
        seqs : list of tuple
            query sequences (Id, sequence)
        prots : dict of list of dict, optional
            hit tables, only relevant when amet = lookup

        Returns
        -------
        dict
            Id-to-score map

        Notes
        -----
        Workflow:
        1. If amet = lookup, just look up, and raise if any sequences don't
        have self-hits.
        2. If amet = fast, run fast built-in algorithm on each sequence.
        3. If amet = native, find the corresponding search method.
        4. Run self-alignment in batches only when method = precomp, otherwise
        the query sequences are already subsetted into batches.
        5. If some sequences don't have self hits in batch self-alignments,
        try to get them via single self-alignments.
        6. If some sequences still don't have self hits, do built-in algorithm,
        but note that the output may be slightly different from others.
        7. If some sequences still don't have self hits, raise.
        """
        res = []

        # just look up (will fail if some are not found)
        if self.aln_method == 'lookup':
            res = self.lookup_selfaln(seqs, prots)

        # use built-in algorithm
        elif self.aln_method == 'fast':
            for id_, seq in seqs:
                bitscore, evalue = self.fast_selfaln(seq)
                res.append((id_, bitscore, evalue))

        # use the same search method for self-alignment
        elif self.aln_method == 'native':

            # divide sequences into batches, when search results are
            # precomputed
            batches = ([seqs] if self.method != 'precomp' else
                       self.subset_seqs(seqs, self.queries, self.maxchars))

            # do self-alignments in batches to save compute
            for batch in batches:
                res_ = []

                # call search method
                if self.method == 'remote':
                    res_ = self.remote_selfaln(batch)
                    sleep(self.delay)
                elif self.method == 'blast':
                    res_ = self.blast_selfaln(batch)
                elif self.method == 'diamond':
                    res_ = self.diamond_selfaln(batch)

                # merge results
                res += res_

        # if some hits are not found, do single alignments
        left = set([x[0] for x in seqs]) - set([x[0] for x in res])
        if left:
            print('WARNING: The following sequences cannot be self-aligned '
                  'in a batch. Do individual alignments instead.')
            print('  {}'.format(', '.join(left)))
            for id_, seq in seqs:
                if id_ not in left:
                    continue
                res_ = None

                # call search method
                if self.method == 'remote':
                    res_ = self.remote_selfaln([(id_, seq)])
                    sleep(self.delay)
                elif self.method == 'blast':
                    res_ = self.blast_selfaln([(id_, seq)])
                elif self.method == 'diamond':
                    res_ = self.diamond_selfaln([(id_, seq)])

                # if failed, do built-in alignment
                if not res_:
                    print('WARNING: Sequence {} cannot be self-aligned '
                          'using the native method. Do fast alignment '
                          'instead.'.format(id_))
                    bitscore, evalue = self.fast_selfaln(seq)
                    res_ = [(id_, bitscore, evalue)]

                # merge results
                res += res_

            # check if all sequences have results
            left = set([x[0] for x in seqs]) - set([x[0] for x in res])
            if left:
                raise ValueError('Cannot calculate self-alignment metrics for '
                                 'the following sequences:\n  {}'.format(
                                     ', '.join(sorted(left))))
        return {x[0]: x[1] for x in res}

    """input/output functions"""

    @staticmethod
    def subset_seqs(seqs, queries=None, maxchars=None):
        """Generate subsets of sequences based on cutoffs.

        Parameters
        ----------
        seqs : list of tuple
            sequences to subset (id, sequence)
        queries : int, optional
            number of query sequences per subset
        maxchars : int, optional
            maximum total length of query sequences per subset

        Returns
        -------
        list
            subsets

        Raises
        ------
        If any sequence exceeds maxchars.
        """
        if not maxchars:

            # no subsetting
            if not queries:
                return [seqs]

            # subsetting only by queries
            subsets = []
            for i in range(0, len(seqs), queries):
                subsets.append(seqs[i:i + queries])
            return subsets

        # subsetting by maxchars, and by queries if applicable
        subsets = [[]]
        cquery, cchars = 0, 0
        for id_, seq in seqs:
            chars = len(seq)
            if chars > maxchars:
                raise ValueError('Sequence {} exceeds maximum allowed length '
                                 '{} for search.'.format(id_, maxchars))
            if cchars + chars > maxchars or queries == cquery > 0:
                subsets.append([])
                cquery, cchars = 0, 0
            subsets[-1].append((id_, seq))
            cquery += 1
            cchars += chars
        return subsets

    def update_search_results(self, prots, res, indices=set()):
        """Update proteins with new search results.

        Parameters
        ----------
        prots : list of dict
            proteins to update
        res : dict
            search results
        indices : set of int, optional
            indices of proteins to be updated
            if omitted, only proteins with hits will be updated
        """
        for i, prot in enumerate(prots):
            if 'hits' in prot:
                continue
            if indices and i not in indices:
                continue
            id_ = prot['id']
            if id_ in res:
                prot['hits'] = []
                n = 0
                for hit in res[id_]:
                    prot['hits'].append(hit)
                    n += 1
                    if self.maxhits and n == self.maxhits:
                        break
            elif indices and i in indices:
                prot['hits'] = []

    @staticmethod
    def write_search_results(f, prots, indices=None):
        """Write search results to a file.

        Parameters
        ----------
        f : file handle
            file to write to (in append mode)
        prots : array of hash
            protein set
        indices : list of int, optional
            limit to these proteins
        """
        for i in indices if indices else range(len(prots)):
            prot = prots[i]
            f.write('# ID: {}\n'.format(prot['id']))
            f.write('# Length: {}\n'.format(len(prot['seq'])))
            f.write('# Product: {}\n'.format(prot['product']))
            f.write('# Score: {}\n'.format(prot['score']))
            f.write('# Hits: {}\n'.format(len(prot['hits'])))
            for hit in prot['hits']:
                f.write('{}\n'.format('\t'.join([hit[x] for x in (
                    'id', 'identity', 'evalue', 'score', 'coverage',
                    'taxid')])))

    @staticmethod
    def parse_prev_results(fp, prots):
        """Parse previous search results.

        Parameters
        ----------
        file : str
            file containing search results
        prots : list
            protein records

        Returns
        -------
        list of str
            completed protein Ids
        """
        done = []
        with open(fp, 'r') as f:
            for line in f:
                if line.startswith('# ID: '):
                    done.append(line[6:].rstrip('\r\n'))
        doneset = set(done)
        for prot in prots:
            if prot['id'] in doneset:
                prot['score'] = 0
                prot['hits'] = []
        return done

    @staticmethod
    def check_missing_seqs(data):
        """Get a list of proteins whose sequences remain to be retrieved.

        Parameters
        ----------
        data : dict
            protein sets

        Returns
        -------
        list of str
            Ids of proteins without sequences
        """
        res = set()
        for sid, sample in data.items():
            if 'done' in sample:
                continue
            for prot in sample['prots']:
                if not prot['seq'] and 'hits' not in prot:
                    res.add(prot['id'])
        return sorted(res)

    def update_dmp_files(self, ids):
        """Write added taxonomic information to custom taxdump files.

        Parameters
        ----------
        ids : list of str
            added taxIds

        Notes
        -----
        Taxonomic information will be appended to nodes.dmp and names.dmp in
        the working directory.
        """
        with open(join(self.output, 'nodes.dmp'), 'a') as fo:
            with open(join(self.output, 'names.dmp'), 'a') as fa:
                for id_ in sorted(ids, key=int):
                    fo.write('{}\t|\t{}\t|\t{}\t|\n'.format(
                        id_, self.taxdump[id_]['parent'],
                        self.taxdump[id_]['rank']))
                    fa.write('{}\t|\t{}\t|\n'.format(
                        id_, self.taxdump[id_]['name']))

    """sequence query functions"""

    def blast_seqinfo(self, ids):
        """Retrieve information of given sequence Ids from local BLAST database.

        Parameters
        ----------
        ids : list of str
            query sequence Ids

        Returns
        -------
        list of tuple
            (id, taxid, product, sequence)

        Notes
        -----
        When making database (using makeblastdb), one should do -parse_seqids
        to enable search by name (instead of sequence) and -taxid_map with a
        seqId-to-taxId map to enable taxId query.
        """
        # run blastdbcmd
        # fields: accession, taxid, sequence, title
        cmd = '{} -db {} -entry {} -outfmt "%a %T %s %t"'.format(
            self.blastdbcmd, self.db, ','.join(ids))
        out = run_command(cmd)[1]

        # parse output
        res = []
        header = True
        for line in out:
            # catch invalid database error
            if header:
                # letter case is dependent on BLAST version
                if line.lower().startswith('blast database error'):
                    raise ValueError('Invalid BLAST database: {}.'
                                     .format(self.db))
                header = False

            # if one sequence Id is not found, program will print:
            #   Error: [blastdbcmd] Entry not found: NP_123456.1
            # if none of sequence Ids are found, program will print:
            #   Error: [blastdbcmd] Entry or entries not found in BLAST
            #   database
            if (line.startswith('Error') or 'not found' in line or
                    line.startswith('Please refer to')):
                continue

            # limit to 4 partitions because title contains spaces
            x = line.split(None, 3)

            # if database was not compiled with -taxid_map, taxIds will be 0
            if x[1] in ('0', 'N/A'):
                x[1] = ''

            # title will be empty if -parse_seqids was not triggered
            if len(x) == 3:
                x.append('')

            # parse title to get product
            else:
                x[3] = get_product(x[3])

            res.append((x[0], x[1], x[3], x[2]))
        return res

    def remote_seqinfo(self, ids):
        """Retrieve information of given sequence Ids from remote server.

        Parameters
        ----------
        ids : list of str
            query sequence Ids (e.g., accessions)

        Returns
        -------
        list of tuple
            (id, taxid, product, sequence)

        Raises
        ------
        - All sequence Ids are invalid.
        - Failed to retrieve info from server.
        """
        return self.parse_fasta_xml(self.remote_fetches(
            ids, 'db=protein&rettype=fasta&retmode=xml&id={}'))

    def remote_fetches(self, ids, urlapi):
        """Fetch information from remote server in batch mode

        Parameters
        ----------
        ids : list of str
            query entries (e.g., accessions)
        urlapi : str
            URL API

        Returns
        -------
        str
            fetched information

        Notes
        -----
        The function dynamically determines the batch size, starting from a
        large number and reducing by half on every other retry. This is because
        the NCBI server is typically busy and frequently runs into the "502 Bad
        Gateway" issue. To resolve, one may subset queries and retry.
        """
        cq = self.fetch_queries  # current number of queries
        cids = ids  # current list of query Ids
        res = ''
        while True:
            batches = [cids[i:i + cq] for i in range(0, len(cids), cq)]
            failed = []

            # batch fetch sequence information
            for batch in batches:
                try:
                    res += self.remote_fetch(urlapi.format(','.join(batch)))
                    print('  Fetched information of {} entries.'.format(
                        len(batch)), flush=True)
                    sleep(self.fetch_delay)
                except ValueError:
                    failed.extend(batch)

            # reduce batch size by half on each trial
            if failed and cq > 1:
                cids = failed
                cq = int(cq / 2)
                print('Retrying with smaller batch size...', flush=True)
            else:
                cids = []
                break
        if cids:
            print('WARNING: Cannot retrieve information of {} entries.'
                  .format(len(cids)))
        return res

    def remote_fetch(self, urlapi):
        """Fetch information from remote server.

        Parameters
        ----------
        urlapi : str
            URL API

        Returns
        -------
        str
            fetched information

        Raises
        ------
        ValueError
            fetch failed
        """
        url = '{}?{}'.format(self.fetch_server, urlapi)
        for i in range(self.fetch_retries):
            if i:
                print('Retrying...', end=' ', flush=True)
                sleep(self.fetch_delay)
            try:
                with urlopen(url, timeout=self.fetch_timeout) as response:
                    return response.read().decode('utf-8')
            except (HTTPError, URLError) as e:
                print('{} {}.'.format(e.code, e.reason), end=' ', flush=True)
        print('', flush=True)
        raise ValueError('Failed to fetch information from remote server.')

    def update_prot_seqs(self, seqs):
        """Update protein sets with retrieved sequences.

        Parameters
        ----------
        seqs : list of tuple
            protein sequences (id, taxid, product, sequence)

        Returns
        -------
        int
            number of proteins with sequence added

        Notes
        ------
        Different protein sets may contain identical protein Ids.
        """
        # hash proteins by id
        prots = {x[0]: (x[2], x[3]) for x in seqs}

        # if queries are accessions without version, NCBI will add version
        acc2ver = {}
        for id_, info in prots.items():
            acc_ = re.sub(r'\.\d+$', '', id_)
            acc2ver[acc_] = id_

        n = 0
        for sid, sample in self.data.items():
            for prot in sample['prots']:
                if prot['seq']:
                    continue

                # try to match protein Id (considering de-versioned accession)
                id_ = prot['id']
                if id_ not in prots:
                    try:
                        id_ = acc2ver[id_]
                    except KeyError:
                        continue
                if id_ not in prots:
                    continue

                # update protein information
                for i, key in enumerate(['product', 'seq']):
                    if not prot[key]:
                        prot[key] = prots[id_][i]
                n += 1
        return n

    @staticmethod
    def parse_fasta_xml(xml):
        """Parse sequence information in FASTA/XML format retrieved from NCBI
        server.

        Parameters
        ----------
        xml : str
            sequence information in XML format

        Returns
        -------
        list of [str, str, str, str]
            id, taxid, product, sequence

        Notes
        -----
        NCBI EFectch record type = TinySeq XML
        Refer to:
        - https://www.ncbi.nlm.nih.gov/books/NBK25499/table/chapter4.T._valid_
        values_of__retmode_and/?report=objectonly
        Example RESTful API:
        - https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?db=protein
        &rettype=fasta&retmode=xml&id=NP_454622.1,NP_230502.1,NP_384288.1
        """
        seqs = []
        for m in re.finditer(r'<TSeq>(.+?)<\/TSeq>', xml, re.DOTALL):
            s_ = m.group(1)
            seq = []
            for key in (('accver', 'taxid', 'defline', 'sequence')):
                m_ = re.search(r'<TSeq_%s>(.+)<\/TSeq_%s>' % (key, key), s_)
                seq.append(m_.group(1) if m_ else '')
            seq[2] = get_product(seq[2])
            seqs.append(seq)
        return seqs

    """homology search functions"""

    def blast_search(self, seqs):
        """Run BLAST to search a sequence against a database.

        Parameters
        ----------
        seqs : list of tuple
            query sequences (id, sequence)

        Returns
        -------
        dict of list of dict
            hit table per query sequence

        Raises
        ------
        - If BLAST run fails.

        Notes
        -----
        - In ncbi-blast+ 2.7.1, the standard tabular format (-outfmt 6) is:
            qaccver saccver pident length mismatch gapopen qstart qend sstart
            send evalue bitscore
        - In older versions, fields 1 and 2 are qseqid and sseqid. The
            difference is that an sseqid may read "ref|NP_123456.1|" instead of
            "NP_123456.1".
        - staxids are ;-delimited, will be "N/A" if not found or the database
            does not contain taxIds.
        - The BLAST database should ideally be prepared as:
            makeblastdb -in seqs.faa -dbtype prot -out db -parse_seqids \
                -taxid map seq2taxid.txt
        - Unlike blastn, blastp does not have -perc_identity.
        """
        tmpin = join(self.tmpdir, 'tmp.in')
        with open(tmpin, 'w') as f:
            write_fasta(seqs, f)
        cmd = '{} -query {} -db {}'.format(self.blastp, tmpin, self.db)
        args = {x: getattr(self, x, None) for x in (
            'evalue', 'coverage', 'maxseqs', 'threads', 'extrargs')}
        if args['evalue']:
            cmd += ' -evalue {}'.format(args['evalue'])
        if args['coverage']:
            cmd += ' -qcov_hsp_perc {}'.format(args['coverage'])
        if args['maxseqs']:
            cmd += ' -max_target_seqs {}'.format(args['maxseqs'])
        if args['threads'] is not None:
            cmd += ' -num_threads {}'.format(args['threads'])
        if args['extrargs']:
            cmd += ' {}'.format(args['extrargs'])
        cmd += (' -outfmt "6 qaccver saccver pident evalue bitscore qcovhsp'
                ' staxids"')
        ec, out = run_command(cmd)
        if ec:
            raise ValueError('blastp failed with error code {}.'.format(ec))
        remove(tmpin)
        return self.parse_def_table(out)

    def diamond_search(self, seqs):
        """Run DIAMOND to search a sequence against a database.

        Parameters
        ----------
        seqs : list of tuple
            query sequences (id, sequence)

        Returns
        -------
        dict of list of dict
            hit table per query sequence

        Raises
        ------
        - If DIAMOND run fails.

        Notes
        -----
        The DIAMOND database should ideally be prepared as:
            diamond makedb --in seqs.faa --db db \
                --taxonmap prot.accession2taxid
        """
        tmpin = join(self.tmpdir, 'tmp.in')
        with open(tmpin, 'w') as f:
            write_fasta(seqs, f)
        cmd = '{} blastp --query {} --db {} --threads {} --tmpdir {}'.format(
            self.diamond, tmpin, self.db, self.threads, self.tmpdir)
        args = {x: getattr(self, x, None) for x in (
            'evalue', 'identity', 'coverage', 'maxseqs', 'extrargs')}
        if args['evalue']:
            cmd += ' --evalue {}'.format(args['evalue'])
        if args['identity']:
            cmd += ' --id {}'.format(args['identity'])
        if args['coverage']:
            cmd += ' --query-cover {}'.format(args['coverage'])
        if args['maxseqs']:
            cmd += ' --max-target-seqs {}'.format(args['maxseqs'])
        if args['extrargs']:
            cmd += ' {}'.format(args['extrargs'])
        cmd += (' --outfmt 6 qseqid sseqid pident evalue bitscore qcovhsp'
                ' staxids')
        ec, out = run_command(cmd, merge=False)
        if ec:
            raise ValueError('diamond failed with error code {}.'.format(ec))
        remove(tmpin)
        return self.parse_def_table(out)

    def remote_search(self, seqs):
        """Perform BLAST search through a remote server.

        Parameters
        ----------
        seqs : list of tuple
            query sequences (id, sequence)

        Returns
        -------
        dict of list of dict
            hit table per query sequence

        Notes
        -----
        - NCBI's official reference of RESTful APIs:
            https://ncbi.github.io/blast-cloud/dev/using-url-api.html
        - NCBI's official sample Perl script:
            https://blast.ncbi.nlm.nih.gov/docs/web_blast.pl
        - NCBI has restrictions on the frequency and bandwidth of remote BLAST
          searches. See this page:
            https://blast.ncbi.nlm.nih.gov/Blast.cgi?CMD=Web&PAGE_TYPE=
            BlastDocs&DOC_TYPE=DeveloperInfo
        - As of 2018, using the NCBI server for genome-scale searches is
          typically slow.
        - Instead, NCBI recommends setting up custom BLAST servers. See:
            https://ncbi.github.io/blast-cloud/
        """
        # generate query URL
        query = ''.join(['>{}\n{}\n'.format(id_, seq) for id_, seq in seqs])
        url = ('{}?CMD=Put&PROGRAM=blastp&DATABASE={}'
               .format(self.server, self.db))
        if self.algorithm:
            url += '&BLAST_PROGRAMS={}'.format(self.algorithm)
        if self.evalue:
            url += '&EXPECT={}'.format(self.evalue)
        if self.maxseqs:
            url += '&MAX_NUM_SEQ={}'.format(self.maxseqs)
        if self.entrez:
            url += '&EQ_TEXT={}'.format(quote(self.entrez))
        if self.extrargs:
            url += '&{}'.format(self.extrargs.lstrip('&'))
        url += '&QUERY={}'.format(quote(query))
        print('Submitting {} queries for search.'.format(len(seqs)),
              end='', flush=True)

        trial = 0
        while True:
            if trial:
                if trial == (self.retries or 0) + 1:
                    raise ValueError('Remote search failed after {} '
                                     'trials.'.format(trial))
                print('Retry {} times.'.format(trial), end='', flush=True)
                sleep(self.delay)
            trial += 1

            # get request Id
            with urlopen(url) as response:
                res = response.read().decode('utf-8')
            m = re.search(r'^    RID = (.*$)', res, re.MULTILINE)
            if not m:
                print('WARNING: Failed to obtain RID.')
                continue
            rid = m.group(1)
            print(' RID: {}.'.format(rid), end='', flush=True)
            sleep(1)

            # check status
            url_ = '{}?CMD=Get&FORMAT_OBJECT=SearchInfo&RID={}'.format(
                self.server, rid)
            starttime = time()
            success = False
            while True:
                with urlopen(url_) as response:
                    res = response.read().decode('utf-8')
                m = re.search(r'\s+Status=(.+)', res, re.MULTILINE)
                if not m:
                    print('WARNING: Failed to retrieve remote search status.')
                    break
                status = m.group(1)
                if status == 'WAITING':
                    if time() - starttime > self.timeout:
                        print('WARNING: Remote search timeout.')
                        break
                    print('.', end='', flush=True)
                    sleep(self.delay)
                    continue
                elif status in ('FAILED', 'UNKNOWN'):
                    print('WARNING: Remote search failed.')
                    break
                elif status == 'READY':
                    if 'ThereAreHits=yes' not in res:
                        print('WARNING: Remote search returned no result.')
                        break
                    success = True
                    break
                else:
                    print('WARNING: Unknown remote search status: {}.'
                          .format(status))
                    break
            if not success:
                continue
            sleep(1)

            # retrieve result
            url_ = ('{}?CMD=Get&ALIGNMENT_VIEW=Tabular&FORMAT_TYPE=Text&RID={}'
                    .format(self.server, rid))
            if self.maxseqs:
                url_ += '&MAX_NUM_SEQ={}&DESCRIPTIONS={}'.format(
                    self.maxseqs, self.maxseqs)
            with urlopen(url_) as response:
                res = response.read().decode('utf-8')
            if '# blastp' not in res or '# Query: ' not in res:
                print('WARNING: Invalid format of remote search results.')
                continue
            print(' Results retrieved.')
            break

        # fields (as of 2018): query acc.ver, subject acc.ver, % identity,
        # alignment length, mismatches, gap opens, q. start, q. end, s. start,
        # s. end, evalue, bit score, % positives
        m = re.search(r'<PRE>(.+?)<\/PRE>', res, re.DOTALL)
        out = m.group(1).splitlines()
        lenmap = {id_: len(seq) for id_, seq in seqs}
        return self.parse_m8_table(out, lenmap)

    def parse_hit_table(self, file, lenmap=None):
        """Determine hit table type and call corresponding parser.

        Parameters
        ----------
        file : str
            hit table file
        lenmap : dict, optional
            map of sequence Ids to lengths (only needed for m8)

        Returns
        -------
        list of dict
            hit table
        """
        ism8 = None
        lines = []
        with open(file, 'r') as f:
            for line in f:
                line = line.rstrip('\r\n')
                if line and not line.startswith('#'):
                    lines.append(line)
                    if ism8 is None:
                        x = line.split('\t')
                        ism8 = len(x) > 8
        return (self.parse_m8_table(lines, lenmap) if ism8 else
                self.parse_def_table(lines))

    def parse_def_table(self, lines):
        """Parse search results in default tabular format.

        Parameters
        ----------
        lines : list of str
            search result in default tabular format
            fields: qseqid sseqid pident evalue bitscore qcovhsp staxids

        Returns
        -------
        dict of list of dict
            hits per query
        """
        res = {}
        ths = {x: getattr(self, x, 0) for x in (
            'evalue', 'identity', 'coverage', 'maxhits')}
        for line in lines:
            line = line.rstrip('\r\n')
            if not line or line.startswith('#'):
                continue
            x = line.split('\t')

            # filter by thresholds
            if ths['evalue']:
                if x[3] != '*' and ths['evalue'] < float(x[3]):
                    continue
            if ths['identity']:
                if x[2] != '*' and ths['identity'] > float(x[2]):
                    continue
            if ths['coverage']:
                if x[5] != '*' and ths['coverage'] > float(x[5]):
                    continue

            # pass if maximum targets reached
            if ths['maxhits']:
                if x[0] in res and ths['maxhits'] == len(res[x[0]]):
                    continue

            # add hit to list
            res.setdefault(x[0], []).append({
                'id': seqid2accver(x[1]), 'identity': x[2], 'evalue': x[3],
                'score': x[4], 'coverage': x[5], 'taxid': x[6] if x[6] not
                in {'', 'N/A', '0'} else ''})
        return res

    def parse_m8_table(self, lines, lenmap):
        """Parse search results in BLAST's standard tabular format (m8).

        Parameters
        ----------
        lines : list of str
            search result in BLAST m8 tabular format
            fields: qseqid sseqid pident length mismatch gapopen qstart qend
                    sstart send evalue bitscore
        lenmap : dict
            map of sequence Ids to lengths (needed for calculating coverage)

        Returns
        -------
        list of dict
            hit table

        Raises
        ------
        - Query Id not found in length map.
        """
        res = {}
        ths = {x: getattr(self, x, 0) for x in (
            'evalue', 'identity', 'coverage', 'maxhits')}
        for line in lines:
            line = line.rstrip('\r\n')
            if not line or line.startswith('#'):
                continue
            x = line.split('\t')

            # calculate coverage
            if x[0] not in lenmap:
                raise ValueError('Invalid query sequence Id: {}.'.format(x[0]))
            try:
                cov = (int(x[7]) - int(x[6]) + 1) / lenmap[x[0]] * 100
            except ValueError:
                cov = 0

            # filter by thresholds
            if ths['evalue']:
                if x[10] != '*' and ths['evalue'] < float(x[10]):
                    continue
            if ths['identity']:
                if x[2] != '*' and ths['identity'] > float(x[2]):
                    continue
            if ths['coverage']:
                if cov and ths['coverage'] > cov:
                    continue

            # pass if maximum targets reached
            if ths['maxhits']:
                if x[0] in res and ths['maxhits'] == len(res[x[0]]):
                    continue

            # add hit to list
            res.setdefault(x[0], []).append({
                'id': seqid2accver(x[1]), 'identity': x[2], 'evalue': x[10],
                'score': x[11], 'coverage': '{:.2f}'.format(cov), 'taxid': ''})
        return res

    """taxonomy query functions"""

    def update_hit_taxids(self, prots, taxmap={}):
        """Update hits with taxIds, and update master sequence Id to taxId map.

        Parameters
        ----------
        prots : dict of list of dict
            proteins (e.g., search results)
        taxmap : dict, optional
            reference sequence Id to taxId map

        Returns
        -------
        list, list
            sequence Ids still without taxIds
            taxIds added to master map
        """
        idsotid = set()  # proteins without taxIds
        newtids = set()  # newly added taxIds

        for prot, hits in prots.items():
            for hit in hits:
                id_, tid = hit['id'], hit['taxid']

                # taxId already in hit table
                if tid:
                    if id_ not in self.prot2tid:
                        self.prot2tid[id_] = tid
                        newtids.add(tid)
                    continue

                # taxId already in taxon map
                try:
                    hit['taxid'] = self.prot2tid[id_]
                    continue
                except KeyError:
                    pass

                # taxId in reference taxon map:
                try:
                    tid = taxmap[id_]
                    hit['taxid'] = tid
                    self.prot2tid[id_] = tid
                    newtids.add(tid)
                    continue
                except KeyError:
                    pass

                # not found
                idsotid.add(id_)
        return sorted(idsotid), sorted(newtids)

    def remote_taxinfo(self, ids):
        """Retrieve complete taxonomy information of given taxIds from remote
        server.

        Parameters
        ----------
        ids : list of str
            query taxIds

        Returns
        -------
        str
            taxonomy information in XML format

        Raises
        ------
        - taxId list is invalid.
        - Failed to retrieve info from server.
        """
        res = self.remote_fetches(ids, 'db=taxonomy&id={}')

        # this error occurs when taxIds are not numeric
        if '<ERROR>ID list is empty' in res:
            raise ValueError('Invalid taxId list.')
        return res

    def parse_taxonomy_xml(self, xml):
        """Parse taxonomy information in XML format retrieved from NCBI server.

        Parameters
        ----------
        xml : str
            taxonomy information in XML format

        Returns
        -------
        list of str
            taxIds added to taxonomy database

        Notes
        -----
        The function will update taxonomy database.
        """
        added = []

        # get result for each query
        p = re.compile(r'<Taxon>\n'
                       r'\s+<TaxId>(\d+)<\/TaxId>\n.+?'
                       r'\s+<ScientificName>([^<>]+)<\/ScientificName>.+?'
                       r'\s+<ParentTaxId>(\d+)<\/ParentTaxId>.+?'
                       r'\s+<Rank>([^<>]+?)<\/Rank>(.+?)\n'
                       r'<\/Taxon>',
                       re.DOTALL | re.VERBOSE)
        for m in p.finditer(xml):
            tid = m.group(1)
            if tid in self.taxdump:
                continue

            # add query taxId to taxdump
            self.taxdump[tid] = {
                'name': m.group(2), 'parent': m.group(3), 'rank': m.group(4)}
            added.append(tid)

            # get lineage
            m1 = re.search(r'<LineageEx>(.+?)<\/LineageEx>', m.group(5),
                           re.DOTALL)
            if not m1:
                continue

            # move up through lineage
            p1 = re.compile(r'\s+<Taxon>\n'
                            r'\s+<TaxId>(\d+)<\/TaxId>\n'
                            r'\s+<ScientificName>([^<>]+)<\/ScientificName>\n'
                            r'\s+<Rank>([^<>]+)<\/Rank>\n'
                            r'\s+<\/Taxon>\n',
                            re.DOTALL | re.VERBOSE)

            for m2 in reversed(list(p1.finditer(m1.group(1)))):
                tid_ = m2.group(1)
                pid = self.taxdump[tid]['parent']
                if pid == '':
                    self.taxdump[tid]['parent'] = tid_
                elif pid != tid_:
                    raise ValueError('Broken lineage for {}: {} <=> {}.'
                                     .format(tid, pid, tid_))
                tid = tid_
                if tid in self.taxdump:
                    continue
                self.taxdump[tid] = {
                    'name': m2.group(2), 'parent': '', 'rank': m2.group(3)}
                added.append(tid)

            # stop at root
            if self.taxdump[tid]['parent'] == '':
                self.taxdump[tid]['parent'] = '1'
        return added

    """self-alignment functions"""

    @staticmethod
    def lookup_selfaln(seqs, hits):
        """Look up self-alignment metrics of sequences from their hit tables.

        Parameters
        ----------
        seqs : list of tuple
            query sequences (id, sequence)
        hits : dict of list of dict
            hit tables

        Returns
        -------
        list of tuple
            (id, bitscore, evalue)
        """
        res = []
        for id_, seq in seqs:
            msg = ('Cannot find a self-hit for sequence {}. Consider setting '
                   'self-alignment method to other than "lookup".'.format(id_))
            if id_ not in hits:
                raise ValueError(msg)
            found = False
            for hit in hits[id_]:
                if hit['id'] == id_:
                    res.append((id_, hit['score'], hit['evalue']))
                    found = True
                    break
            if not found:
                raise ValueError(msg)
        return res

    @staticmethod
    def fast_selfaln(seq):
        """Calculate self-alignment statistics using built-in Python code.

        Parameters
        ----------
        seq : str
            query sequence

        Returns
        -------
        tuple of (str, str)
            bitscore and evalue

        Notes
        -----
        - Statistics are calculated following the official BLAST documentation:
            https://www.ncbi.nlm.nih.gov/BLAST/tutorial/Altschul-1.html
        - Default BLASTp parameters are assumed (matrix = BLOSUM62, gapopen
        = 11, gapextend = 1), except for that the composition based statistics
        is switched off (comp-based-stats = 0).
        - Result should be identical to that by DIAMOND, but will be slightly
        different from that by BLAST.
        """
        # BLOSUM62 is the default aa substitution matrix for BLAST / DIAMOND
        blosum62 = {'A': 4, 'R': 5, 'N': 6,  'D': 6, 'C': 9,
                    'Q': 5, 'E': 5, 'G': 6,  'H': 8, 'I': 4,
                    'L': 4, 'K': 5, 'M': 5,  'F': 6, 'P': 7,
                    'S': 4, 'T': 5, 'W': 11, 'Y': 7, 'V': 4}

        # calculate raw score (S)
        n, raw = 0, 0
        for c in seq.upper():
            try:
                n += 1
                raw += blosum62[c]

            # in case there are non-basic amino acids
            except KeyError:
                pass

        # BLAST's empirical values when gapopen = 11, gapextend = 1. See:
        # ncbi-blast-2.7.1+-src/c++/src/algo/blast/core/blast_stat.c, line #268
        lambda_, K = 0.267, 0.041

        # calculate bit score (S')
        bit = (lambda_ * raw - log(K)) / log(2)

        # calculate e-value (E)
        e = n ** 2 * 2 ** -bit

        return '{:.1f}'.format(bit), '{:.3g}'.format(e)

    def blast_selfaln(self, seqs):
        """Run BLAST to align sequences to themselves.

        Parameters
        ----------
        seqs : list of tuple
            query sequences (id, sequence)

        Returns
        -------
        list of tuple
            (id, bitscore, evalue)
        """
        tmpin = join(self.tmpdir, 'tmp.in')
        with open(tmpin, 'w') as f:
            write_fasta(seqs, f)
        cmd = '{} -query {} -subject {} -num_threads {} -outfmt 6'.format(
            self.blastp, tmpin, tmpin, self.threads)
        extrargs = getattr(self, 'extrargs', None)
        if extrargs:
            cmd += ' {}'.format(extrargs)
        ec, out = run_command(cmd)
        if ec:
            raise ValueError('blastp failed with error code {}.'.format(ec))
        remove(tmpin)
        return(self.parse_self_m8(out))

    def diamond_selfaln(self, seqs):
        """Run DIAMOND to align sequences to themselves.

        Parameters
        ----------
        seqs : list of tuple
            query sequences (id, sequence)

        Returns
        -------
        list of tuple
            (id, bitscore, evalue)
        """
        # generate temporary query file
        tmpin = join(self.tmpdir, 'tmp.in')
        with open(tmpin, 'w') as f:
            write_fasta(seqs, f)

        # generate temporary database
        tmpdb = join(self.tmpdir, 'tmp.dmnd')
        cmd = '{} makedb --in {} --db {} --threads {} --tmpdir {}'.format(
            self.diamond, tmpin, tmpdb, self.threads, self.tmpdir)
        ec, out = run_command(cmd, merge=False)
        if ec:
            raise ValueError('diamond failed with error code {}.'.format(ec))

        # perform search
        cmd = '{} blastp --query {} --db {} --threads {} --tmpdir {}'.format(
            self.diamond, tmpin, tmpdb, self.threads, self.tmpdir)
        extrargs = getattr(self, 'extrargs', None)
        if extrargs:
            cmd += ' {}'.format(extrargs)
        ec, out = run_command(cmd, merge=False)
        if ec:
            raise ValueError('diamond failed with error code {}.'.format(ec))

        remove(tmpin)
        remove(tmpdb)
        return(self.parse_self_m8(out))

    def remote_selfaln(self, seqs):
        """Perform BLAST search through a remote server.

        Parameters
        ----------
        seqs : list of tuple
            query sequences (id, sequence)

        Returns
        -------
        list of tuple
            (id, bitscore, evalue)
        """
        # further split sequences into halves (to comply with URI length limit)
        batches = self.subset_seqs(seqs, maxchars=int(
            (self.maxchars + 1) / 2)) if self.maxchars else [seqs]
        result = []
        for batch in batches:

            # generate query URL
            query = ''.join(['>{}\n{}\n'.format(id_, seq)
                             for id_, seq in batch])
            query = quote(query)
            url = ('{}?CMD=Put&PROGRAM=blastp&DATABASE={}&QUERY={}&SUBJECTS={}'
                   .format(self.aln_server, self.db, query, query))
            if self.extrargs:
                url += '&{}'.format(self.extrargs.lstrip('&'))
            print('Submitting {} queries for self-alignment.'.format(
                len(batch)), end='', flush=True)

            trial = 0
            while True:
                if trial:
                    if trial == (self.retries or 0) + 1:
                        raise ValueError('Remote self-alignment failed after '
                                         '{} trials.'.format(trial))
                    print('Retry {} times.'.format(trial), end='', flush=True)
                    sleep(self.delay)
                trial += 1

                # get request Id
                with urlopen(url) as response:
                    res = response.read().decode('utf-8')
                m = re.search(r'^    RID = (.*$)', res, re.MULTILINE)
                if not m:
                    print('WARNING: Failed to obtain RID.')
                    continue
                rid = m.group(1)
                print(' RID: {}.'.format(rid), end='', flush=True)
                sleep(1)

                # check status
                url_ = '{}?CMD=Get&FORMAT_OBJECT=SearchInfo&RID={}'.format(
                    self.aln_server, rid)
                starttime = time()
                success = False
                while True:
                    with urlopen(url_) as response:
                        res = response.read().decode('utf-8')
                    m = re.search(r'\s+Status=(.+)', res, re.MULTILINE)
                    if not m:
                        print('WARNING: Failed to retrieve remote self-'
                              'alignment status.')
                        break
                    status = m.group(1)
                    if status == 'WAITING':
                        if time() - starttime > self.timeout:
                            print('WARNING: Remote self-alignment timeout.')
                            break
                        print('.', end='', flush=True)
                        sleep(self.delay)
                        continue
                    elif status in ('FAILED', 'UNKNOWN'):
                        print('WARNING: Remote self-alignment failed.')
                        break
                    elif status == 'READY':
                        if 'ThereAreHits=yes' not in res:
                            print('WARNING: Remote self-alignment returned no '
                                  'result.')
                            break
                        success = True
                        break
                    else:
                        print('WARNING: Unknown remote self-alignment status: '
                              '{}.'.format(status))
                        break
                if not success:
                    continue
                sleep(1)

                # retrieve result
                url_ = ('{}?CMD=Get&ALIGNMENT_VIEW=Tabular&FORMAT_TYPE=Text&'
                        'RID={}'.format(self.aln_server, rid))
                with urlopen(url_) as response:
                    res = response.read().decode('utf-8')
                if '# blastp' not in res or '# Query: ' not in res:
                    print('WARNING: Invalid format of remote self-alignment '
                          'results.')
                    continue
                print(' Results retrieved.')
                break

            m = re.search(r'<PRE>(.+?)<\/PRE>', res, re.DOTALL)
            out = m.group(1).splitlines()
            result += self.parse_self_m8(out)
        return result

    @staticmethod
    def parse_self_m8(lines):
        """Extract self-alignment results from m8 format table.

        Parameters
        ----------
        lines : list of str
            hit table in BLAST m8 format
            fields: qseqid sseqid pident length mismatch gapopen qstart qend
            sstart send evalue bitscore

        Returns
        -------
        list of tuple
            hit table (id, bitscore, evalue)
        """
        res = []
        used = set()
        for line in lines:
            x = line.rstrip('\r\n').split('\t')
            if x[0].startswith('#'):
                continue
            if len(x) < 12:
                continue
            if x[0] != x[1]:
                continue
            if x[0] in used:
                continue
            res.append((x[1], x[11], x[10]))
            used.add(x[0])
        return res
