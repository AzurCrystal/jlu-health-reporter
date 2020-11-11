#!/usr/bin/env python3
import os, sys, re, json, logging as log, threading, urllib3, requests
from time import time, sleep
import queue
DEBUG = 0#+1
CONFIG = sys.argv[1] if len(sys.argv)>1 else 'config.json' # take cli arg or default
CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)), CONFIG) # relative to file
# CONFIG = '/etc/jlu.conf' # force a config file here
RETRIES = 100
TIMEOUT = 2
INTERVAL = 2
MAX_USERS = 64
MAX_THREADS = 10

class taskRunner(threading.Thread):
	def __init__(self,queue:queue.Queue) -> None:
		threading.Thread.__init__(self)
		self.queue = queue
		self.sigkill = False

	def stop(self):
		self.sigkill = True

	def run(self):
		while not self.sigkill:
			try:
				task = self.queue.get(block=True, timeout=10)
			except queue.Empty:
				self.sigkill=True
				break
			result = runTask(task)
			self.queue.task_done()
			if result:
				if self.queue.qsize()>0:
					log.info(f"{self.queue.qsize} tasks left.")
					continue
				elif self.queue.qsize()==0:
					self.sigkill=True
					break
			self.queue.put(task,block=True,timeout=None)
			log.info(f"{task['transaction']}:{task['username']} failed, re-appending task.")

def runTask(task) -> bool:
	for _ in range(RETRIES):
		try:
			s = requests.Session()
			s.headers.update({'Referer': 'https://ehall.jlu.edu.cn/'})
			s.verify = False
			
			log.info('Authenticating...')
			r = s.get('https://ehall.jlu.edu.cn/jlu_portal/login', timeout=TIMEOUT)
			pid = re.search('(?<=name="pid" value=")[a-z0-9]{8}', r.text)[0]
			log.debug(f"PID: {pid}")
			postPayload = {'username': task['username'], 'password': task['password'], 'pid': pid}
			r = s.post('https://ehall.jlu.edu.cn/sso/login', data=postPayload, timeout=TIMEOUT)

			log.info('Requesting form...')
			r = s.get(f"https://ehall.jlu.edu.cn/infoplus/form/{task['transaction']}/start", timeout=TIMEOUT)
			csrfToken = re.search('(?<=csrfToken" content=").{32}', r.text)[0]
			log.debug(f"CSRF: {csrfToken}")
			postPayload = {'idc': task['transaction'], 'csrfToken': csrfToken}
			r = s.post('https://ehall.jlu.edu.cn/infoplus/interface/start', data=postPayload, timeout=TIMEOUT)
			sid = re.search('(?<=form/)\\d*(?=/render)', r.text)[0]
			log.debug(f"Step ID: {sid}")
			postPayload = {'stepId': sid, 'csrfToken': csrfToken}
			r = s.post('https://ehall.jlu.edu.cn/infoplus/interface/render', data=postPayload, timeout=TIMEOUT)
			data = json.loads(r.content)['entities'][0]

			log.info('Submitting form...')
			for k, v in task['fields'].items():
				if eval(task['conditions'].get(k, 'True')):
					data['data'][k] = v
			postPayload = {
				'actionId': 1,
				'formData': json.dumps(data['data']),
				'nextUsers': '{}',
				'stepId': sid,
				'timestamp': int(time()),
				'boundFields': ','.join(data['fields'].keys()),
				'csrfToken': csrfToken
			}
			log.debug(f"Payload: {postPayload}")
			r = s.post('https://ehall.jlu.edu.cn/infoplus/interface/doAction', data=postPayload, timeout=TIMEOUT)
			log.debug(f"Result: {r.text}")
			if json.loads(r.content)['ecode'] != 'SUCCEED' :
				raise Exception('The server returned a non-successful status.')
			log.info(f"{task['transaction']}:{task['username']} Success!")
			return True
		except Exception as e:
			log.error(e)
			sleep(TIMEOUT)
	log.error('Failed too many times, exiting...')
	return False

log.basicConfig(
	level=log.INFO-10*DEBUG,
	format='%(asctime)s %(threadName)s:%(levelname)s %(message)s'
)
log.warning('Started.')
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
taskList = queue.Queue(MAX_USERS)
log.info(f'Reading config from {CONFIG}')
config = json.load(open(CONFIG))
for task in config.get('tasks', [{}]):
	for k in ['username', 'password', 'transaction']:
		task.setdefault(k, config.get(k))
	for k in ['fields', 'conditions']:
		task[k] = {**config.get(k, {}), **task.get(k, {})}
	if task['transaction']:
		taskList.put(task,block=True,timeout=None)
		log.info(f"{task['transaction']}:{task['username']} queued.")
for _ in range(MAX_THREADS):
	taskRunner(taskList).start()
	sleep(INTERVAL)
taskList.join()
log.info('All Completed.')