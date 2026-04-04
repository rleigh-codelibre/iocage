# Copyright (c) 2014-2019, iocage
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted providing that the following conditions
# are met:
# 1. Redistributions of source code must retain the above copyright
#    notice, this list of conditions and the following disclaimer.
# 2. Redistributions in binary form must reproduce the above copyright
#    notice, this list of conditions and the following disclaimer in the
#    documentation and/or other materials provided with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY THE AUTHOR ``AS IS'' AND ANY EXPRESS OR
# IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
# WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED.  IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR ANY
# DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
# DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS
# OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION)
# HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT,
# STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING
# IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.
"""build module for the cli."""

import click

import iocage_lib.ioc_common as ioc_common
import iocage_lib.iocage as ioc

__rootcmd__ = True


@click.command(
    name="build",
    help="Build a template from a RapSheet.json file."
)
@click.option("--file", "-f", "rapsheet", default="RapSheet.json",
              help="Path to the RapSheet file.")
@click.option("--name", "-n", default=None,
              help="Name for the resulting template.")
@click.option("--tag", "-t", "tag", default=None,
              help="Tag for the resulting template (e.g., myapp:v1.0).")
@click.option("--no-cache", "no_cache", is_flag=True, default=False,
              help="Do not use cache when building.")
@click.option("--keep-build-jail", "keep_build_jail", is_flag=True,
              default=False,
              help="Keep the build jail on failure for debugging.")
@click.argument("props", nargs=-1)
def cli(rapsheet, name, tag, no_cache, keep_build_jail, props):
    """Build a template from a RapSheet.json file."""
    try:
        ioc.IOCage(skip_jails=True).build(
            rapsheet,
            name=name,
            tag=tag,
            no_cache=no_cache,
            keep_build_jail=keep_build_jail,
            props=props
        )
    except Exception as e:
        ioc_common.logit({
            'level': 'EXCEPTION',
            'message': str(e)
        })
