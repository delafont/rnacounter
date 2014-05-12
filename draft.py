
import pysam
import os, sys
import itertools
from numpy import asarray
import copy
import math


Ecounter = itertools.count(1)

def parse_gtf(row):
    # GTF fields = ['chr','source','name','start','end','score','strand','frame','attributes']
    def _score(x):
        if str(x) == '.': return 0
        else: return float(x)
    def _strand(x):
        smap = {'+':1, 1:1, '-':-1, -1:-1, '.':0, 0:0}
        return smap[x]
    if not row: return
    row = row.strip().split("\t")
    if len(row) < 9:
        raise ValueError("\"Attributes\" field required in GFF.")
    if row[2] != 'exon':
        return False
    attrs = (x.strip().split() for x in row[8].split(';'))  # {gene_id: "AAA", ...}
    attrs = dict((x[0],x[1].strip("\"")) for x in attrs)
    exon_id = attrs.get('exon_id', 'E%d'%Ecounter.next())
    return Exon(id=exon_id, gene_id=attrs['gene_id'], gene_name=attrs['gene_name'],
                chrom=row[0], start=int(row[3])-1, end=int(row[4]),
                name=exon_id, score=_score(row[5]), strand=_strand(row[6]),
                transcripts=[attrs['transcript_id']])


class GenomicObject(object):
    def __init__(self, id='',gene_id='',gene_name='',chrom='',start=0,end=0,
                 name='',score=0.0,strand=0,length=0,seq='',multiplicity=1):
        self.id = id
        self.gene_id = gene_id
        self.gene_name = gene_name
        self.chrom = chrom
        self.start = start
        self.end = end
        self.name = name
        self.score = score
        self.strand = strand
        self.length = length
        #self.seq = seq  # sequence
        self.multiplicity = multiplicity
    def __and__(self,other):
        """The intersection of two GenomicObjects"""
        assert self.chrom==other.chrom, "Cannot add features from different chromosomes"
        selfid = (self.id,) if isinstance(self.id,int) else self.id
        otherid = (other.id,) if isinstance(other.id,int) else other.id
        return self.__class__(
            id = selfid + otherid,
            gene_id = '|'.join(set([self.gene_id, other.gene_id])),
            gene_name = '|'.join(set([self.gene_name, other.gene_name])),
            chrom = self.chrom,
            #start = max(self.start, other.start),
            #end = min(self.end, other.end),
            ##   name = '|'.join(set([self.name, other.name])),
            name = '|'.join([self.name, other.name]),
            #score = self.score + other.score,
            strand = (self.strand + other.strand)/2,
            #length = min(self.end, other.end) - max(self.start, other.start),
            multiplicity = self.multiplicity + other.multiplicity
        )
    def __repr__(self):
        return "<%s (%d-%d) %s>" % (self.name,self.start,self.end,self.gene_name)

class Exon(GenomicObject):
    def __init__(self, transcripts=set(), **args):
        GenomicObject.__init__(self, **args)
        self.transcripts = transcripts   # list of transcripts it is contained in
        self.length = self.end - self.start
    def __and__(self,other):
        E = GenomicObject.__and__(self,other)
        E.transcripts = set(self.transcripts) | set(other.transcripts)
        return E

class Transcript(GenomicObject):
    def __init__(self, exons=[], **args):
        GenomicObject.__init__(self, **args)
        self.exons = exons               # list of exons it contains


def intersect_exons_list(feats, multiple=False):
    """The intersection of a list *feats* of GenomicObjects.
    If *multiple* is True, permits multiplicity: if the same exon E1 is
    given twice, there will be "E1|E1" parts. Otherwise pieces are unique."""
    if multiple is False:
        feats = list(set(feats))
    if len(feats) == 1:
        return copy.deepcopy(feats[0])
    else:
        return reduce(lambda x,y: x&y, feats)


def cobble(exons, multiple=False):
    """Split exons into non-overlapping parts.
    :param multiple: see intersect_exons_list()."""
    ends = [(e.start,1,e) for e in exons] + [(e.end,0,e) for e in exons]
    ends.sort()
    active_exons = []
    cobbled = []
    for i in xrange(len(ends)-1):
        a = ends[i]
        b = ends[i+1]
        if a[1]==1:
            active_exons.append(a[2])
        elif a[1]==0:
            active_exons.remove(a[2])
        if len(active_exons)==0:
            continue
        if a[0]==b[0]:
            continue
        e = intersect_exons_list(active_exons)
        e.start = a[0]; e.end = b[0];
        cobbled.append(e)
    return cobbled


def isnum(s):
    """Return True if string *s* represents a number, False otherwise"""
    try:
        float(s)
        return True
    except ValueError:
        return False

class Counter(object):
    def __init__(self, stranded=False):
        self.n = 0 # read count
        self.n_raw = 0 # read count, no NH flag
        self.n_ws = 0 # read count, wrong strand
        self.strand = 0 # exon strand
        if stranded:
            self.count_fct = self.count_stranded
        else:
            self.count_fct = self.count

    def __call__(self, alignment):
        self.count_fct(alignment)

    def count(self, alignment):
        NH = [1.0/t[1] for t in alignment.tags if t[0]=='NH']+[1]
        self.n += NH[0]
        self.n_raw += 1

    def count_stranded(self, alignment):
        NH = [1.0/t[1] for t in alignment.tags if t[0]=='NH']+[1]
        if self.strand == 1 and alignment.is_reverse == False \
        or self.strand == -1 and alignment.is_reverse == True:
            self.n += NH[0]
        else:
            self.n_ws += NH[0]


# Gapdh id: ENSMUSG00000057666
# Gapdh transcripts: ENSMUST00000147954, ENSMUST00000147954, ENSMUST00000118875
#                    ENSMUST00000073605, ENSMUST00000144205, ENSMUST00000144588


######################################################################

def process_chunk(ckexons, sam, chrom, lastend):
    """Distribute counts across transcripts and genes of a chunk *ckexons*
    of non-overlapping exons."""

    if ckexons[0].gene_name != "Gapdh": return 1


    #--- Regroup occurrences of the same Exon from a different transcript
    exons = []
    for key,group in itertools.groupby(ckexons, lambda x:x.id):
        # ckexons are sorted because chrexons were sorted by chrom,start,end
        exon0 = group.next()
        for g in group:
            exon0.transcripts.append(g.transcripts[0])
        exons.append(exon0)
    del ckexons


    #--- Convert chromosome name
    if chrom[:3] == "NC_" : pass
    elif isnum(chrom): chrom = "chr"+chrom


    #--- Get all reads from this chunk
    allcounter = Counter()
    sam.fetch(chrom, exons[0].start, lastend, callback=allcounter)
    print "Total: (raw) %d - (NH) %d" % (allcounter.n_raw, allcounter.n)


    #--- Cobble all these intervals
    pieces = cobble(exons)


    #--- Filter out too similar transcripts,
    # e.g. made of the same exons up to 100bp.
    t2e = {}                               # map {transcript: [pieces IDs]}
    for p in pieces:
        if p.length < 100: continue        # filter out cobbled pieces of less that read length
        for t in p.transcripts:
            t2e.setdefault(t,[]).append(p.id)
    e2t = {}
    for t,e in t2e.iteritems():
        es = tuple(sorted(e))              # combination of pieces indices
        e2t.setdefault(es,[]).append(t)    # {(pieces IDs combination): [transcripts with same struct]}
    # Replace too similar transcripts by the first of the list, arbitrarily
    transcripts = set()  # full list of remaining transcripts
    tx_replace = dict((badt,tlist[0]) for tlist in e2t.values() for badt in tlist[1:] if len(tlist)>1)
    for p in pieces:
        filtered = set([tx_replace.get(t,t) for t in p.transcripts])
        transcripts |= filtered
        p.transcripts = list(filtered)
    transcripts = list(transcripts)


    #--- Remake the transcript-pieces mapping
    tp_map = {}
    for p in pieces:
        for tx in p.transcripts:
            tp_map.setdefault(tx,[]).append(p.name)
    #--- Remake the transcripts-exons mapping
    te_map = {}  # map {transcript: [exons]}
    for exon in exons:
        txs = exon.transcripts
        for t in txs:
            te_map.setdefault(t,[]).append(exon)
    #transcripts = te_map.keys()
    print transcripts


    #--- Build the structure matrix : lines are exons, columns are transcripts,
    # so that A[i,j]!=0 means "transcript Tj contains exon Ei".
    # A[i,j] is 1/(number of exons of Tj).
    #Avals = asarray([[float(t in p.transcripts) for t in transcripts] for p in pieces])
    Avals = asarray([[float(t in p.transcripts)/len(tp_map[t]) for t in transcripts] for p in pieces])


    #--- Count reads in each piece - normalize etc.
    tp_map = {}  # map {transcript: [pieces of exons]}
    pcounter = Counter()
    for p in pieces:
        p.length = p.end - p.start
        sam.fetch(chrom, p.start,p.end, callback=pcounter)
        p.score = 1000 * pcounter.n_raw / float(p.length)
        pcounter.n_raw = 0
        txs = p.transcripts
        for t in txs:
            tp_map.setdefault(t,[]).append(p)


    print Avals



def rnacount(bamname, annotname):
    """Annotation in GTF format, assumed to be sorted at least w.r.t. chrom name."""
    sam = pysam.Samfile(bamname, "rb")
    annot = open(annotname, "r")

    row = annot.readline().strip()
    exon0 = parse_gtf(row)
    chrom = exon0.chrom
    lastchrom = chrom

    while row:

        # Load all GTF exons of a chromosome in memory and sort
        chrexons = []
        while chrom == lastchrom:  # start <= lastend and
            exon = parse_gtf(row)
            if exon.end - exon.start > 1 :
                chrexons.append(exon)
            row = annot.readline().strip()
            if not row:
                break
        lastchrom = chrom
        chrexons.sort(key=lambda x: (x.start,x.end))
        print ">> Chromosome", chrom

        # Process chunks of overlapping exons / exons of the same gene
        lastend = chrexons[0].end
        lastgeneid = ''
        ckexons = []
        for exon in chrexons:
            # Store
            if (exon.start <= lastend) or (exon.gene_id == lastgeneid):
                ckexons.append(exon)
            # Process the stored chunk of exons
            else:
                process_chunk(ckexons, sam, chrom, lastend)
                ckexons = [exon]
            lastend = max(exon.end,lastend)
            lastgeneid = exon.gene_id
        process_chunk(ckexons, sam, chrom, lastend)

    annot.close()
    sam.close

######################################################################

bamname = "testfiles/gapdhKO.bam"
annotname = "testfiles/mm9_mini.gtf"

rnacount(bamname,annotname)










                #if attrs['gene_name']=='Gapdh':
                #    print "--------------------"
                #print "processed up to", lastend, ":"
                #for k,v in ckexons:
                #    print v['exon_id'], v['transcript_id']
                #if attrs['gene_name']=='Gapdh':
                #    print "--------------------"







#class Gene(GenomicObject):
#    def __init__(self, exons=set(),transcripts=set(), **args):
#        GenomicObject.__init__(self, **args)
#        self.exons = exons               # list of exons contained
#        self.transcripts = transcripts   # list of transcripts contained



    #if 0 and 'ENSMUST00000147954' in te_map:
    #    print '-------ENSMUST00000147954'
    #    for x in te_map['ENSMUST00000147954']: print x
    #    print;
    #    for x in tp_map['ENSMUST00000147954']: print x
    #    print '-------ENSMUST00000117757'
    #    for x in te_map['ENSMUST00000117757']: print x
    #    print
    #    for x in tp_map['ENSMUST00000117757']: print x
    #    print '-------ENSMUST00000118875'
    #    for x in te_map['ENSMUST00000118875']: print x
    #    print
    #    for x in tp_map['ENSMUST00000118875']: print x
    #    print '-------ENSMUST00000073605'
    #    for x in te_map['ENSMUST00000073605']: print x
    #    print
    #    for x in tp_map['ENSMUST00000073605']: print x
    #    print '-------ENSMUST00000144205'
    #    for x in te_map['ENSMUST00000144205']: print x
    #    print
    #    for x in tp_map['ENSMUST00000144205']: print x
    #    print '-------ENSMUST00000144588'
    #    for x in te_map['ENSMUST00000144588']: print x
    #    print
    #    for x in tp_map['ENSMUST00000144588']: print x
    #    print '-------'


    #if exon.gene_name == "Gapdh":
    #    for e in ckexons: print e

    #testexons = [e for e in ckexons if e.name[-4:] in ['5781','9315']]
    #for e in testexons: print e
    #print '------------'
    #testpieces = cobble(testexons)
    #for p in testpieces: print p



    #if exon.gene_name == "Gapdh":
    #    for p in pieces: print p

