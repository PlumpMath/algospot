# -*- coding: utf-8 -*-
from django.conf import settings
from celery.decorators import task
from models import Submission, Attachment
import urllib
import urlparse
import os
import zipfile
import glob
import sandbox
import languages

@task()
def add(x, y):
    return x + y

@task()
def judge_submission(server, submission):
    logger = judge_submission.get_logger()

    def copy(source, dest):
        while True:
            chunk = source.read(1024*1024)
            if not chunk: break
            dest.write(chunk)

    def download(server, attachment, destination):
        # TODO: add MD5 verification to downloaded files
        full = urlparse.urljoin(server, attachment.file.url)
        logger.info("downloading %s ..", server)
        copy(urllib.urlopen(full), open(destination, "wb"))

    def unzip(archive, data_dir):
        logger.info("unzipping %s ..", archive)
        file = zipfile.ZipFile(archive, "r")
        for name in file.namelist():
            dest = os.path.join(data_dir, os.path.basename(name))
            logger.info("generating %s ..", dest)
            copy(file.open(name), open(dest, "wb"))

    def download_data(server, problem):
        attachments = Attachment.objects.filter(problem=problem)
        if not os.path.exists(data_dir):
            os.makedirs(data_dir)
        for entry in attachments:
            basename = os.path.basename(entry.file.name)
            ext = basename.split(".")[-1].lower()
            if ext not in ["in", "out", "zip"]: continue
            destination = os.path.join(data_dir, basename)
            # TODO: check MD5 and make sure we don't have to download again
            if not os.path.exists(destination):
                download(server, entry, destination)
                if ext == "zip":
                    unzip(destination, data_dir)
            else:
                logger.info("We already have %s", basename)

    def get_ioset():
        io = {}
        for file in glob.glob(os.path.join(data_dir, "*")):
            if file.endswith(".in") or file.endswith(".out"):
                tokens = file.split(".")
                basename = ".".join(tokens[:-1])
                if basename not in io:
                    io[basename] = {}
                io[basename][tokens[-1]] = file
        if not io:
            raise Exception(u"채점 데이터가 없습니다.")
        for key, value in io.iteritems():
            if len(value) != 2:
                raise Exception(u"입출력 파일 목록이 쌍이 맞지 않습니다."
                                u"목록:\n%s" % str(io))
        return io

    sandbox_env = None
    try:
        # 언어별 채점 모듈 존재 여부부터 확인하기
        if submission.language not in languages.modules:
            raise Exception(u"언어 %s의 채점 모듈을 찾을 수 없습니다." %
                            submission.language)
        language_module = languages.modules[submission.language]

        # 문제 채점 데이터를 다운받고 채점 준비
        problem = submission.problem
        data_dir = os.path.join(settings.JUDGE_SETTINGS["WORKDIR"],
                                "data/%d-%s" % (problem.id, problem.slug))
        download_data(server, submission.problem)
        ioset = get_ioset()

        # 샌드박스 생성
        sandbox_env = sandbox.get_sandbox(problem.memory_limit)

        # 컴파일
        submission.state = Submission.COMPILING
        submission.save()
        result = language_module.setup(sandbox_env, submission.source)
        if result["status"] != "ok":
            submission.state = Submission.COMPILE_ERROR
            submission.message = result["message"]
            submission.save()

        # set sandbox in copy-on-write mode: will run
        sandbox_env.mount_home("cow")

        # let's run now
        for io in ioset.iteritems():
            result = language_module.run(sandbox_env, io["in"], problem.time_limit,
                                         problem.memory_limit)


    except Exception as e:
        submission.state = Submission.CANT_BE_JUDGED
        submission.message = (u"채점 중 오류가 발생했습니다. 오류 메시지:\n" +
                              unicode(e))
        submission.save()
    finally:
        if sandbox_env:
            sandbox_env.teardown()

