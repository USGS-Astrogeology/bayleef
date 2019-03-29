import subprocess as sps
import sys, os, time
import logging

logger = logging.getLogger('Bayleef')

"""
borrowed from https://github.com/Kelvinrr/pysbatch/blob/master/pysbatch/pysbatch.py
Until an anaconda recipe can be made

"""


def sbatch(job_name="py_job", mem='8', cpus=1, dep="", time='3-0', log="submit.out",wrap="python hello.py", add_option=""):
    sub=['sbatch',
         '--ntasks=1',
         '--cpus-per-task={}'.format(cpus), '-N', '1',
         '--job-name={}'.format(job_name),
         '--mem={}'.format(mem+"000"),
         '--time={}'.format(time),
         dep, add_option,
         '--out={}'.format(log)]
    sub.append('--wrap="{}"'.format(wrap.strip()))
    # print(" ".join(sub))
    command = " ".join(sub)
    logger.info(command)
    process = sps.Popen(command, shell=True, stdout=sps.PIPE)
    stdout = process.communicate()[0].decode("utf-8")
    return(stdout)


def run_cmd(cmd):
    # simplified subprocess.run() of running linux command in python
    # cmd pass in as a list of strings, i.e. cd .. should be ['cd', '..']
    # return screen print as a string
    process=sps.run(cmd, stdout=sps.PIPE)
    return process.stdout.decode("utf-8")


def wait_for_jobs(jobs):
    # get the normal amount of lines in a default empty query
    default_nlines = len(run_cmd(['squeue', '-u', os.environ['USER'], "--name=\"\""]).split("\n"))
    jobflag = "--name=" + ",".join(jobs)
    while len(run_cmd(['squeue', '-u', os.environ['USER'], jobflag]).split("\n"))-default_nlines > 0:
        time.sleep(1)


def limit_jobs(limit=20000):
    default_nlines = len(run_cmd(['squeue', '-u', os.environ['USER'], "--name=\"\""]).split("\n"))

    # limit the total number of jobs in slurm job queue
    while len(run_cmd(['squeue', '-u', os.environ['USER'], '-t', 'running']).split("\n")) >= limit:
        time.sleep(1)


def limit_queue(limit=20000):
    # limit the total number of jobs in slurm job queue
    while len(run_cmd(['squeue', '-u', os.environ['USER']]).split("\n")) >= limit:
        time.sleep(1)
