import csv, requests, urllib

###############################################################################
# Frame represents a 2-D array of data, uniform in each column.  The data may
# be local, or may be in an H2O cluster.  The data is loaded from a CSV file,
# and is either a python-process-local file or cluster-local file, or a list of
# Vecs (columns of data).
class H2OFrame(object):
  def __init__(self, localFName=None, remoteFName=None, vecs=None):
    # Read a CSV file
    if remoteFName:             # Read remotely into cluster
      if not H2OCONN: raise ValueError("No open h2o connection")
      rawkey = H2OCONN.ImportFile(remoteFName)
      setup  = H2OCONN.ParseSetup(rawkey)
      parse  = H2OCONN.Parse(setup)
      hexKey = parse['hex']['name']
      cols   = parse['columnNames']
      frames = H2OCONN.Frame(hexKey)
      rows   = frames['frames'][0]['rows']
      self._vecs = [Vec(str(col),Expr(hexKey,idx,str(col),rows))  for idx,col in enumerate(cols)]
      print "Imported",remoteFName,"into cluster as",hexKey,"with",rows,"rows and",len(cols),"cols"
    elif localFName:            # Read locally into python process
      with open(localFName, 'rb') as csvfile:
        self._vecs = []
        for name in csvfile.readline().split(','): 
          self._vecs.append(Vec(name.rstrip(), Expr([])))
        for row in csv.reader(csvfile):
          for i,data in enumerate(row):
            self._vecs[i].append(data)
      print "Imported",localFName,"into local python process"
    # Construct from an array of Vecs already passed in
    elif vecs is not None:
      vlen = len(vecs[0])
      for v in vecs:  
        if not isinstance(v,Vec): raise ValueError("Not a list of Vecs")
        if len(v)!=vlen: raise ValueError("Vecs not the same size, "+str(vlen)+" vs "+str(len(v)))
      self._vecs = vecs
    else: raise ValueError("Frame made from CSV file or an array of Vecs only")

  # Print [col, cols...]
  def __str__(self): return self._vecs.__repr__()

  # Column selection via integer, string (name) returns a Vec
  # Column selection via slice returns a subset Frame
  def __getitem__(self,i):
    if isinstance(i,int): return self._vecs[i]
    if isinstance(i,str):
      for v in self._vecs:  
        if i==v._name: return v
      raise ValueError("Name "+i+" not in Frame")
    # Slice; return a Frame not a Vec
    if isinstance(i,slice): return H2OFrame(vecs=self._vecs[i])
    raise NotImplementedError

  # Number of columns
  def __len__(self): return len(self._vecs)

  # Addition
  def __add__(self,i):
    if len(self)==0: return self
    if isinstance(i,H2OFrame):
      if len(i) != len(self):
        raise ValueError("Frame len()="+len(self)+" cannot be broadcast across len(i)="+len(i))
      return H2OFrame(vecs=[x+y for x,y in zip(self._vecs,i._vecs)])
    if isinstance(i,Vec):
      if len(i) != len(self._vecs[0]):
        raise ValueError("Vec len()="+len(self._vecs[0])+" cannot be broadcast across len(i)="+len(i))
      return H2OFrame(vecs=[x+i for x in self._vecs])
    if isinstance(i,int):
      return H2OFrame(vecs=[x+i for x in self._vecs])
    raise NotImplementedError

  def __radd__(self,i): return self+i  # Add is associative


##############################################################################
# A single column of uniform data, possibly lazily computed
# 
class Vec(object):
  def __init__(self, name, expr):
    assert isinstance(name,str)
    assert isinstance(expr,Expr)
    self._name = name  # String
    self._expr = expr  # Always an expr
    expr._name = name  # Pass name along to expr

  # Append a value during CSV read, convert to float
  def append(self,data):
    __x__ = data
    try: __x__ = float(data)
    except ValueError: pass
    self._expr._data.append(__x__)

  # Print self
  def __repr__(self): return self._name+" "+self._expr.__str__()

  # Basic indexed or sliced lookup
  def __getitem__(self,i): return self._expr[i]

  # Basic (broadening) addition
  def __add__(self,i):
    if isinstance(i,Vec):       # Vec+Vec
      if len(i) != len(self):
        raise ValueError("Vec len()="+len(self)+" cannot be broadcast across len(i)="+len(i))
      return Vec(self._name+"+"+i._name,Expr("+",self,i))
    if isinstance(i,(int,float)): # Vec+int
      if i==0: return self        # Additive identity
      return Vec(self._name+"+"+str(i),Expr("+",self,Expr(i)))
    raise NotImplementedError

  def __radd__(self,i): return self+i  # Add is associative

  def mean(self):
    return Expr("mean",self._expr,None)

  # Number of rows
  def __len__(self): return len(self._expr)

  def __del__(self):
    # Vec is dead, so this Expr is unused by the python interpreter (but might
    # be used in some other larger computation)
    self._expr._name = "TMP_"+self._name


##############################################################################
#
# Exprs - 
# - A pending to-be-computed BigData expression.  Does not have a Key
# - An already computed BigData expression.  Does have a Key
# - A small-data computation, pending or not.
#
# Pending computations point to other Exprs in a DAG of pending computations.
# Pointed at by at most one Vec (during construction) and no others.  If that
# Vec goes dead, this computation is known to be an internal tmp; used only in
# building other Exprs.
# 
class Expr(object):
  # Constructor choices:
  # ( "op"  left rite) - pending calc, awaits left & rite being computed
  # ( data  None None) - precomputed local small data
  # (hexkey #num name) - precomputed remote  Big Data
  def __init__(self,op,left=None,rite=None,length=None):
    self._op,self._data = (op,None) if isinstance(op,str) else ("rawdata",op)
    self._name = self._op       # Set an initial name, generally overwritten
    assert self.isLocal() or self.isRemote() or self.isPending()
    self._left = left._expr if isinstance(left,Vec) else left
    self._rite = rite._expr if isinstance(rite,Vec) else rite
    assert self._left is None or isinstance(self._left,Expr) or isinstance(self._data,unicode), self.debug()
    assert self._rite is None or isinstance(self._rite,Expr) or isinstance(self._data,unicode), self.debug()
    # Compute length eagerly
    if self.isRemote():
      assert length is not None
      self._len  = length
    elif self.isLocal():
      self._len = len(self._data) if isinstance(self._data,list) else 1
    else:
      self._len = len(self._left)
    assert self._len is not None

  def isLocal   (self): return isinstance(self._data,(list,int,float))
  def isRemote  (self): return isinstance(self._data,unicode)
  def isPending (self): return self._data==None
  def isComputed(self): return not self.isPending()

  # Length, generally withOUT triggering an eager evaluation
  def __len__(self):  return self._len

  # Print structure without eval'ing
  def debug(self):
    return ("(["+self._name+"] = "+
            str(self._left._name if isinstance(self._left,Expr) else self._left)+
            " "+self._op+" "+
            str(self._rite._name if isinstance(self._rite,Expr) else self._rite)+
            " = "+str(type(self._data))+")")

  # Eval and print
  def __repr__(self):
    x = self.eager()
    if self.isLocal():  return x.__str__()
    # Big Data result.  Download the first bits only before printing.
    print "Download "+str(x)
    frames = H2OCONN.Frame(x)
    print frames
    raise NotImplementedError

  # Basic indexed or sliced lookup
  def __getitem__(self,i): 
    x = self.eager()
    if self.isLocal(): return x[i]
    if not isinstance(i,int): raise NotImplementedError  # need a bigdata slice here
    # ([ $frame #row #col)
    j = H2OCONN.Rapids("([ $"+str(self._data)+" #"+str(i)+" '"+str(self._rite)+"')")
    return j['scalar']

  # Small-data add; result of a (lazy but small) Expr vs a plain int/float
  def __add__ (self,i): return self.eager()+i
  def __radd__(self,i): return self+i  # Add is associative

  def __del__(self):
    if self.isPending() or self.isLocal(): return  # Dead pending op or local data; nothing to delete
    assert self.isRemote()
    global _CMD;  
    if _CMD is None:
      H2OCONN.Rapids("(del $"+str(self._data)+" '"+str(self._rite)+"')")
    else:
      s = "DELE: "+self._name+" key="+self._rite+ "; "
      _CMD += s  # Tell cluster to delete temp as part of larger expression
      raise NotImplementedError

  def eager(self):
    if self._data is None:
      global _CMD; assert not _CMD;  _CMD = "{";  self._doit();  print _CMD+"}"; _CMD = None
    return self._data

  # External API for eager; called by all top-level demanders (e.g. print)
  # May trigger (recursive) big-data eval.
  def _doit(self):
    if self.isComputed(): return
    left = self._left
    rite = self._rite
    if isinstance(left,Expr): left._doit()
    if isinstance(rite,Expr): rite._doit()

    if self._op == "+":
      if isinstance(left._data,(int,float)):
        if isinstance(rite._data,(int,float)):  # Small data
          lname, rname = None,None  
          self._data = left+rite
        else:
          lname, rname = str(left), rite._name
          self._data = [left+x for x in rite._data]
      elif isinstance(rite._data,(int,float)):
        lname, rname = left._name, str(rite)
        if left.isLocal():  # Small data
          self._data = [x+rite for x in left._data]
        else:                   # Big Data + scalar
          # (+ ([ $frame "null" #col) rite)
          j = H2OCONN.Rapids("(+ ([ $"+str(left._data)+" \"null\" #"+str(left._left)+") #"+str(rite)+")")
          self._data, left, rite = j['key']['name'], 0, ""
      else:
        lname, rname = left._name, rite._name
        self._data = [x+y for x,y in zip(left._data,rite._data)]
    elif self._op == "mean":
      lname, rname = left._name, None
      self._data = sum(left._data)/len(left._data)  # Stores a small data result
    else:
      raise NotImplementedError
    if lname:
      global _CMD
      _CMD += self._name+"= "+lname+" "+self._op+" "+str(rname)+"; "
    assert self._data is not None
    self._left = None # Trigger GC/ref-cnt of temps
    self._rite = None
    return

# Global list of pending expressions and deletes to ship to the cluster
_CMD = None


##############################################################################
#
# Cluster connection
#
H2OCONN = None # Default connection
class H2OConnection(object):
  def __init__(self,ip="localhost",port=54321):
    assert isinstance(port,int) and 0 <= port <= 65535
    self._ip = ip
    self._port = port
    cld = self.connect()
    ncpus=0;  mmax=0
    for n in cld['nodes']:
      ncpus += n['num_cpus']
      mmax  += n['max_mem']
    print "Connected to cloud '"+cld['cloud_name']+"' size",cld['cloud_size'],"ncpus",ncpus,"maxmem",get_human_readable_size(mmax)
    global H2OCONN
    H2OCONN = self              # Default connection is last openned

  # Dumb url prefix
  def url(self):  return "http://"+self._ip+":"+str(self._port)+"/"

  # Does not actually "connect", instead simply tests that the cluster can be
  # reached, is of a certain size, and is taking basic status commands
  def connect(self,size=1):
    while True:
      cld = self.doSafeGet(self.buildURL("Cloud",{}))
      if not cld['cloud_healthy']:
        raise ValueError("Cluster reports unhealthy status",cld)
      if cld['cloud_size'] >= size and cld['consensus']: return cld
      # Cloud too small or voting in progress; sleep; try again
      time.sleep(0.1)

  # Import a single file; very basic error checking
  # Returns h2o Key
  def ImportFile(self,path):
    j = self.doSafeGet(self.buildURL("ImportFiles",{'path':path}))
    if j['fails']:  raise ValueError("ImportFiles of "+path+" failed on "+j['fails'])
    return j['keys'][0]

  # Return basic parse setup object
  def ParseSetup(self,rawkey):
    # Unable to use 'requests.params=' syntax because it flattens array
    # parameters, but ParseSetup really expects a real array of Keys.
    j = self.doSafeGet(self.buildURL("ParseSetup",{'srcs':[rawkey]}))
    if not j['isValid']: raise ValueError("ParseSetup not Valid",j)
    return j

  # Trigger a parse; blocking
  def Parse(self,setup):
    # Some initial parameters
    p = {'delete_on_done':True,'blocking':True,'hex':setup['hexName']}
    # Copy selected keys
    for key in ['ncols','sep','columnNames','pType','checkHeader','singleQuotes']:
      p[key] = setup[key]
    # Extract only 'name' from each src in the array of srcs
    p['srcs'] = [src['name'] for src in setup['srcs']]
    # Request blocking parse
    # TODO: POST vs GET
    j = self.doSafeGet(self.buildURL("Parse",p))
    if j['job']['status'] != 'DONE': raise ValueError("Parse status expected to be DONE, instead is "+j['job']['status'])
    if j['job']['progress'] != 1.0: raise ValueError("Parse progress expected to be 1.0, instead is "+j['job']['progress'])
    return j

  # Fire off a Rapids expression
  def Rapids(self,expr):
    return self.doSafeGet(self.buildURL("Rapids",{"ast":urllib.quote(expr)}))

  # Return basic Frame info
  def Frame(self,hexKey,off=0,length=10):
    return self.doSafeGet(self.buildURL("3/Frames/"+hexKey+"/columns",{"offset":off,"len":length}))

  # "Safe" REST calls.  Check for errors in a common way
  def doSafeGet(self,url):
    r = requests.get(url)
    # Missing a non-json response check, e.g. 404 check here
    j = r.json()
    if 'errmsg' in j: raise ValueError(j['errmsg'])
    return j

  # function to build a URL from a base and a dictionary of params.  'request'
  # has such a thing but it flattens lists and we need the actual list
  # complete with '[]'
  def buildURL(self,base,params):
    s = self.url()+base+".json"
    sep = '?'
    for k,v in params.items():
      s += sep + k + "="
      if isinstance(v,list):
        sep2 = '['
        for l in v:
          s += sep2 + str(l).encode('utf-8')
          sep2 = ','
        s += ']'
      else:
        s += str(v).encode('utf-8')
      sep = '&'
    return s


# Simple stackoverflow pretty-printer for big numbers
def get_human_readable_size(num):
  exp_str = [ (0, 'B'), (10, 'KB'),(20, 'MB'),(30, 'GB'),(40, 'TB'), (50, 'PB'),]               
  i = 0
  while i+1 < len(exp_str) and num >= (2 ** exp_str[i+1][0]):
    i += 1
    rounded_val = round(float(num) / 2 ** exp_str[i][0], 2)
  return '%s %s' % (rounded_val, exp_str[i][1])
