"""One-time read-only SSH discovery. Credentials never go to the terminal."""
import json
import re
import subprocess

from .config import AutoarrError, private_json

REMOTE = r'''
import json,subprocess,xml.etree.ElementTree as E
request=json.loads(INPUT)
host=request['host']
apps={};problems=[]
def read(container,path):
 return subprocess.check_output(['sudo','-n','docker','exec',container,'cat',path],stderr=subprocess.DEVNULL,timeout=20)
for name,container,port,prefix in [
 ('radarr','ix-radarr-radarr-1',30025,'/api/v3'),
 ('sonarr','ix-sonarr-sonarr-1',30113,'/api/v3'),
 ('lidarr','ix-lidarr-lidarr-1',30071,'/api/v1'),
 ('prowlarr','ix-prowlarr-prowlarr-1',30050,'/api/v1'),
 ('bookshelf','bookshelf',30045,'/api/v1')]:
 try:
  xml=E.fromstring(read(container,'/config/config.xml'))
  base='/'+(xml.findtext('UrlBase') or '').strip('/') if (xml.findtext('UrlBase') or '').strip('/') else ''
  apps[name]={'kind':name,'url':'http://'+host+':'+str(port)+base,'api_prefix':prefix,'api_key':xml.findtext('ApiKey')}
 except Exception as e:problems.append({'app':name,'error':type(e).__name__})
try:
 settings=json.loads(read('ix-seerr-seerr-1','/app/config/settings.json'))
 apps['seerr']={'kind':'seerr','url':'http://'+host+':30357','api_key':settings['main']['apiKey']}
 apps['jellyfin']={'kind':'jellyfin','url':'http://'+host+':30013'+settings['jellyfin'].get('urlBase','').rstrip('/'),'api_key':settings['jellyfin']['apiKey']}
except Exception as e:problems.append({'app':'seerr/jellyfin','error':type(e).__name__})
try:
 code='import sys,json;sys.path.insert(0,"/app/bin/libs");import yaml;d=yaml.safe_load(open("/config/config/config.yaml"));print(json.dumps({"base":d["general"]["base_url"],"key":d["auth"]["apikey"]}))'
 d=json.loads(subprocess.check_output(['sudo','-n','docker','exec','ix-bazarr-bazarr-1','python','-c',code],stderr=subprocess.DEVNULL,timeout=20))
 apps['bazarr']={'kind':'bazarr','url':'http://'+host+':30046'+d['base'].rstrip('/'),'api_key':d['key']}
except Exception as e:problems.append({'app':'bazarr','error':type(e).__name__})
# This stack already allows its LAN clients to access qBittorrent's Web API.
# No authentication policy or application setting is changed here.
apps['qbittorrent']={'kind':'qbittorrent','url':'http://'+host+':30024','auth':'none'}
try:
 code='import sqlite3,json;c=sqlite3.connect("file:/app/db/Kapowarr.db?mode=ro",uri=True);print(json.dumps(dict(c.execute("SELECT key,value FROM config WHERE key IN (\'api_key\',\'url_base\')"))))'
 d=json.loads(subprocess.check_output(['sudo','-n','docker','exec','ix-kapowarr-kapowarr-1','python3','-c',code],stderr=subprocess.DEVNULL,timeout=20))
 if d.get('api_key'):apps['kapowarr']={'kind':'kapowarr','url':'http://'+host+':30093'+d.get('url_base','').rstrip('/'),'api_key':d['api_key']}
except Exception as e:problems.append({'app':'kapowarr','error':type(e).__name__})
print(json.dumps({'version':1,'apps':apps,'discovery':{'excluded':['additional-lidarr-instances'],'problems':problems}}))
'''


def bootstrap(ssh, host, path, overwrite=False):
    if path.exists() and not overwrite:
        raise AutoarrError('Configuration already exists; use config add or explicitly pass --overwrite')
    if not re.fullmatch(r'[A-Za-z0-9_.@:\[\]-]+', ssh) or ssh.startswith('-'):
        raise AutoarrError('Invalid SSH destination')
    if not re.fullmatch(r'[A-Za-z0-9_.:\[\]-]+', host) or host.startswith('-'):
        raise AutoarrError('Invalid HTTP host')
    code = REMOTE.replace('INPUT', repr(json.dumps({'host': host})), 1)
    try:
        p = subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
                            '--', ssh, 'python3', '-'], input=code, text=True,
                           capture_output=True, timeout=240)
    except (OSError, subprocess.TimeoutExpired):
        raise AutoarrError('SSH discovery could not complete; no app configuration was changed') from None
    if p.returncode:
        raise AutoarrError('SSH discovery failed; requires the existing SSH key and passwordless sudo docker access')
    try:
        data = json.loads(p.stdout)
    except ValueError:
        raise AutoarrError('Invalid SSH discovery response; credentials were not printed or saved') from None
    if not data.get('apps'):
        raise AutoarrError('No applications were discovered')
    private_json(path, data)
    return {'config': str(path), 'apps': sorted(data['apps']),
            'discovery': data['discovery'], 'transport': 'direct HTTP; SSH bootstrap only'}
