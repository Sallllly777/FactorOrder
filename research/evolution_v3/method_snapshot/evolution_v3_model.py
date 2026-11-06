"""V3 persisted model calls; unknown use stops the affected run."""
from evolution_v2_evaluate import read,write
from evolution_v3_runtime import atomic_write as write,unknown_reserve
from evolution_api_probe import request
from evolution_v3_generate import payload
import json
import hashlib

def call_model(folder,arm,seed,generation,phase,slots,history,feedback=None):
    folder.mkdir(parents=True,exist_ok=True)
    ledgerfile=folder/'ledger.json'
    ledger=read(ledgerfile) if ledgerfile.exists() else dict(calls=[],reported_total_tokens=0)
    phase_id=f'g{generation}_{phase}'
    existing=next((c for c in reversed(ledger['calls']) if c['phase_id']==phase_id),None)
    if existing:
        responsefile=folder/f"response_{existing['number']:02d}.json"
        if existing['status']!='completed' or not responsefile.exists():
            raise RuntimeError('Unresolved model request; automatic retry forbidden: '+phase_id)
        return read(responsefile)['choices'][0]['message']['content']
    body=payload(arm,seed,generation,phase,slots,history,feedback)
    encoded=json.dumps(body,ensure_ascii=False).encode('utf-8')
    reservation=len(encoded)+8192+4096
    if len(ledger['calls'])>=12 or sum(c['generation']==generation for c in ledger['calls'])>=2:
        raise RuntimeError('Model call budget exhausted')
    if ledger['reported_total_tokens']+unknown_reserve(folder,ledger)+reservation>120000: raise RuntimeError('Model token budget exhausted')
    number=len(ledger['calls'])+1
    write(folder/f'request_{number:02d}.json',body)
    entry=dict(number=number,phase_id=phase_id,generation=generation,status='reserved',
               request_sha256=hashlib.sha256(encoded).hexdigest(),reservation=reservation)
    upper=sum(len(m['content'].encode('utf-8')) for m in body['messages'])+128
    entry.update(input_token_estimate=upper,estimate_method='conservative_utf8_bytes_plus_128',
                 input_target_exceeded=upper>6000)
    ledger['calls'].append(entry);write(ledgerfile,ledger)
    try:
        response=request('/chat/completions',body)
        safe={k:response.get(k) for k in ('id','model','usage','choices')}
        write(folder/f'response_{number:02d}.json',safe)
        usage=response.get('usage',{}).get('total_tokens')
        if type(usage)!=int or usage<0:
            entry['status']='unknown_usage';raise RuntimeError('Missing API usage; stop')
        ledger['reported_total_tokens']+=usage
        entry.update(usage=usage,status='completed',returned_model=response.get('model'))
        if ledger['reported_total_tokens']+unknown_reserve(folder,ledger)>120000:
            entry['status']='budget_exceeded';raise RuntimeError('Actual API usage exceeded cap')
        return response['choices'][0]['message']['content']
    except Exception as error:
        if entry['status']=='reserved': entry['status']='failed_or_uncertain'
        entry['error_type']=type(error).__name__
        raise
    finally: write(ledgerfile,ledger)
