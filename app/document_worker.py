"""Credential-free, resource-bounded text worker. Never imports application state."""
import base64,csv,io,json,os,re,sys


def constrain():
    if os.name=='posix':
        import resource
        resource.setrlimit(resource.RLIMIT_AS,(384*1024*1024,384*1024*1024))
        resource.setrlimit(resource.RLIMIT_CPU,(5,5))
        resource.setrlimit(resource.RLIMIT_NOFILE,(32,32))
    elif os.name=='nt':
        import ctypes
        from ctypes import wintypes as w
        class Basic(ctypes.Structure):
            _fields_=[('process_time',ctypes.c_longlong),('job_time',ctypes.c_longlong),('flags',w.DWORD),
                ('min_ws',ctypes.c_size_t),('max_ws',ctypes.c_size_t),('active',w.DWORD),('affinity',ctypes.c_size_t),('priority',w.DWORD),('scheduling',w.DWORD)]
        class IO(ctypes.Structure):_fields_=[(n,ctypes.c_ulonglong) for n in ('ro','wo','oo','rt','wt','ot')]
        class Extended(ctypes.Structure):_fields_=[('basic',Basic),('io',IO),('process_memory',ctypes.c_size_t),('job_memory',ctypes.c_size_t),('peak_process',ctypes.c_size_t),('peak_job',ctypes.c_size_t)]
        k=ctypes.WinDLL('kernel32',use_last_error=True)
        k.CreateJobObjectW.restype=w.HANDLE;k.CreateJobObjectW.argtypes=[ctypes.c_void_p,w.LPCWSTR]
        k.SetInformationJobObject.argtypes=[w.HANDLE,ctypes.c_int,ctypes.c_void_p,w.DWORD]
        k.AssignProcessToJobObject.argtypes=[w.HANDLE,w.HANDLE];k.GetCurrentProcess.restype=w.HANDLE
        job=k.CreateJobObjectW(None,None);limits=Extended();limits.basic.flags=0x100|0x2|0x8|0x2000
        limits.basic.process_time=5*10_000_000;limits.basic.active=1;limits.process_memory=384*1024*1024
        if not job or not k.SetInformationJobObject(job,9,ctypes.byref(limits),ctypes.sizeof(limits)) or not k.AssignProcessToJobObject(job,k.GetCurrentProcess()):raise RuntimeError('resource isolation unavailable')
        # Keep the job handle alive for the lifetime of this single worker.
        return job
    else:raise RuntimeError('resource isolation unavailable')


def extract(raw,mime):
    segments=[];characters=0;truncated=False
    def add(text,locator):
        nonlocal characters,truncated
        text=text.replace('\x00','').strip()
        if not text:return
        remaining=60000-characters
        if remaining<=0 or len(segments)>=800:truncated=True;return
        if len(text)>min(remaining,4000):text=text[:min(remaining,4000)];truncated=True
        segments.append({'locator':locator,'text':text});characters+=len(text)
    if mime=='application/pdf':
        from pypdf import PdfReader
        from pypdf import filters
        filters.ZLIB_MAX_OUTPUT_LENGTH=4_000_000
        reader=PdfReader(io.BytesIO(raw),strict=True)
        if reader.is_encrypted:raise ValueError('encrypted PDF requires a supplied readable copy')
        count=len(reader.pages)
        if count>20:truncated=True
        for page_no in range(min(count,20)):
            page=reader.pages[page_no]
            contents=page.get_contents()
            if contents is not None and len(contents.get_data())>4_000_000:raise ValueError('page content limit exceeded')
            for line_no,line in enumerate((page.extract_text() or '').splitlines(),1):add(line,{'page':page_no+1,'line':line_no})
    elif mime in ('text/plain','text/csv'):
        text=raw.decode('utf-8-sig')
        if mime=='text/csv':
            csv.field_size_limit(60000)
            for row_no,row in enumerate(csv.reader(io.StringIO(text)),1):add(' | '.join(row),{'row':row_no})
        else:
            for line_no,line in enumerate(text.splitlines(),1):add(line,{'line':line_no})
    else:return {'status':'needs_transcription','segments':[],'measurements':[],'reason':'Image-only input requires separately supplied transcription; no OCR assertion.'}
    measurements=[]
    for segment in segments:
        for match in re.finditer(r'(?<![\w.])(-?\d{1,9}(?:\.\d{1,6})?)\s*(USD/(?:m2/year|kWh|year|kg|L|h)|(?:mL|L)/site/cycle|L/site|mL/L|min/site/cycle|cycles/year|kg/head|W/m2|h/day|fraction|sq\s*ft|sqft|ft²|ft2|m²|m2|feet|ft|meters|metres|m|kWh|kW|liters|litres|L)\b',segment['text']):
            measurements.append({'raw':match.group(0),'value_text':match.group(1),'unit_text':match.group(2),'locator':segment['locator'],
                'classification':'extracted_unverified','interpretation':'Unbound textual candidate; not a property measurement or capacity.'})
            if len(measurements)>=200:break
        if len(measurements)>=200:break
    return {'status':'extracted' if segments else 'needs_transcription','segments':segments,'measurements':measurements,'truncated':truncated,
        'scope':'Text order may differ from the visual document; no facts, identity, units or capacity are authenticated.'}


if __name__=='__main__':
    try:
        retained_job=constrain()
        data=json.loads(sys.stdin.buffer.read(750000))
        raw=base64.b64decode(data['content_base64'],validate=True)
        if len(raw)>512*1024:raise ValueError('document limit exceeded')
        result=extract(raw,data['mime']);result['resource_limits_enforced']=True
    except BaseException:
        result={'status':'parser_refused','segments':[],'measurements':[],'reason':'Malformed, encrypted, unsupported or resource-limited document; retain the original and request a readable transcription.'}
    sys.stdout.write(json.dumps(result,ensure_ascii=True))
