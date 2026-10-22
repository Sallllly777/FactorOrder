"""Start the private browser workbench. No credentials are written to disk."""
import argparse
import os
from pathlib import Path
import secrets
import sys
import threading
import webbrowser

ROOT=Path(__file__).resolve().parent
if hasattr(sys.stdout,'reconfigure'):sys.stdout.reconfigure(encoding='utf-8',errors='replace')
if (ROOT/'.analysis_deps').is_dir():sys.path.insert(0,str(ROOT/'.analysis_deps'))


def main():
    parser=argparse.ArgumentParser(description='启动因序网页研究工作台')
    parser.add_argument('--config',default=str(ROOT/'examples'/'service.json'))
    parser.add_argument('--port',type=int,default=8000)
    parser.add_argument('--no-browser',action='store_true')
    args=parser.parse_args()
    if not 1<=args.port<=65535:parser.error('port must be 1..65535')
    token=os.environ.get('PKUQUANT_API_TOKEN')
    if not token:
        token=secrets.token_urlsafe(36);os.environ['PKUQUANT_API_TOKEN']=token
        print('本次服务访问密钥（请复制到网页登录；关闭服务后失效）：',flush=True)
        print(token,flush=True)
    else:print('使用环境变量 PKUQUANT_API_TOKEN 中的访问密钥。',flush=True)
    os.environ['PKUQUANT_SERVICE_CONFIG']=str(Path(args.config).resolve())
    url=f'http://127.0.0.1:{args.port}'
    print('因序网页工作台：'+url,flush=True)
    print('只接受本机连接。按 Ctrl+C 停止服务；重启后需重新登录。',flush=True)
    if not args.no_browser:
        timer=threading.Timer(1.5,lambda:webbrowser.open(url));timer.daemon=True;timer.start()
    import uvicorn
    uvicorn.run('pkuquant.service:app_from_env',factory=True,host='127.0.0.1',port=args.port,workers=1)


if __name__=='__main__':main()
