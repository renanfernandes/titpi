"""One lightweight consumer; camera capture never waits for SSH inference."""

import fcntl
import json
import logging
import os
import signal
import threading

import database
from bird_id import CONFIG_PATH, DEFAULT_PROMPT
from hermes_ssh import DEFAULT_SETTINGS, identify_remote, validate_settings
from identification_queue import IdentificationQueue

log = logging.getLogger('titpi.identification')


def process_job(queue, job, config):
    section = config.get('hermes_ssh', {})
    notify_all = not isinstance(section, dict) or section.get('notify_all', True) is not False
    try:
        settings = validate_settings({**DEFAULT_SETTINGS, **config.get('hermes_ssh', {})})
        prompt = config.get('identification', {}).get('prompt') or DEFAULT_PROMPT
        result = identify_remote(job['photo_path'], settings, prompt)
        publish = (result.get('status') == 'identified' and result.get('name') != 'Unknown'
                   and result.get('common_name', '').casefold() != 'unidentified bird'
                   and result.get('score', 0) >= settings['min_confidence'])
        if notify_all and not publish:
            outcome = result.get('status')
            if outcome == 'identified':
                outcome = ('unknown' if result.get('name') == 'Unknown'
                           or result.get('common_name', '').casefold() == 'unidentified bird'
                           else 'low_confidence')
            result['diagnostic_status'] = outcome
        queue.complete(job['id'], result, publish=publish, notify=notify_all)
        log.info('Job=%s outcome=%s published=%s; original media retained.',
                 job['id'][:12], result.get('status'), publish)
    except Exception as error:
        result = {'status': 'error', 'common_name': None, 'name': None, 'score': 0,
                  'source': 'hermes_ssh'}
        if notify_all:
            result.update(diagnostic_status='error', diagnostic_error=type(error).__name__)
        queue.complete(job['id'], result, notify=notify_all, error=type(error).__name__)
        log.error('Job=%s failed (%s); original media retained.', job['id'][:12], type(error).__name__)


def send_notification(job):
    if job['species'].get('diagnostic_status'):
        from notifier import send_detection_email
    else:
        from notifier_lcd import send_detection_email
    return send_detection_email(job['label'], job['spike_score'], job['photo_path'],
                                job.get('video_path'), job['species'])


def deliver_pending(queue):
    job = queue.claim_notification()
    if job is None:
        return False
    try:
        success = send_notification(job)
    except Exception as error:
        log.error('Job=%s notification failed (%s); not retried.', job['id'][:12], type(error).__name__)
        success = False
    queue.finish_notification(job['id'], bool(success))
    return True


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
    os.makedirs(os.path.dirname(os.path.abspath(database.DB_PATH)), exist_ok=True)
    with open(database.DB_PATH + '.worker.lock', 'a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log.error('An identification worker is already running.')
            return
        database.init_db()
        queue = IdentificationQueue(database.DB_PATH)
        interrupted = queue.recover()
        if interrupted:
            log.warning('%d interrupted jobs marked failed; not resubmitted.', interrupted)
        stopped = threading.Event()
        for event in (signal.SIGTERM, signal.SIGINT):
            signal.signal(event, lambda signum, frame: stopped.set())
        log.info('Persistent identification worker ready.')
        while not stopped.is_set():
            try:
                with open(CONFIG_PATH) as source:
                    config = json.load(source)
                if config.get('identification', {}).get('provider') == 'hermes_ssh':
                    if deliver_pending(queue):
                        continue
                    job = queue.claim()
                    if job is not None:
                        process_job(queue, job, config)
                        continue
            except Exception as error:
                log.error('Worker cycle failed (%s); queue preserved.', type(error).__name__)
            stopped.wait(2)


if __name__ == '__main__':
    main()