.. index:: Building Jails
.. _Building Jails:

Building Jails
==============

iocage provides a :command:`build` command for creating jail templates
from a declarative configuration file, similar to how Docker builds
container images from a Dockerfile. The build system uses ZFS snapshots
as layers, with a caching mechanism that skips unchanged steps on
subsequent builds.

The result of a build is always a **template** -- an immutable jail image
that can be used to create any number of jails with
:command:`iocage create -t`.

.. index:: RapSheet.json
.. _RapSheet Format:

RapSheet.json Format
--------------------

The build configuration is defined in a JSON file called
:file:`RapSheet.json`. This format is consistent with iocage's existing
use of JSON for jail configuration (:file:`config.json`).

A RapSheet has four sections:

.. code-block:: json

   {
       "from": "15.0-RELEASE",
       "metadata": {
           "maintainer": "John Doe <john@example.com>",
           "description": "Nginx web server jail",
           "labels": {
               "version": "1.0",
               "app": "nginx"
           }
       },
       "properties": {
           "allow_raw_sockets": 1,
           "resolver": "/etc/resolv.conf",
           "boot": 1
       },
       "steps": [
           {"env": {"HTTP_PORT": "80", "WORKER_COUNT": "4"}},
           {"run": "pkg install -y nginx"},
           {"run": "sysrc nginx_enable=YES"},
           {"env": {"APP_ENV": "production"}},
           {"run": "mkdir -p /usr/local/www/data"}
       ]
   }

.. index:: RapSheet Sections
.. _RapSheet Sections:

Sections
++++++++

**from** (required)
   The base release or template to build upon. This can be a FreeBSD
   release name (e.g. ``"15.0-RELEASE"``) or the name of an existing
   template. If the specified release has not been fetched, iocage
   attempts to fetch it automatically.

**metadata** (optional)
   Declarative information about the template. This section does not
   affect the build process or layer caching.

   ``maintainer``
      Contact information for the template maintainer (string).

   ``description``
      A human-readable description of the template (string).

   ``labels``
      Arbitrary key-value pairs for classification (object).

**properties** (optional)
   iocage jail properties to apply to the resulting template's
   :file:`config.json`. Any property that can be set with
   :command:`iocage set` can be specified here. For example:

   .. code-block:: json

      {
          "properties": {
              "allow_raw_sockets": 1,
              "mount_devfs": 1,
              "boot": 1,
              "resolver": "/etc/resolv.conf"
          }
      }

   These properties are inherited by jails created from the template.
   They can be overridden at jail creation time with
   :command:`iocage create -t mytemplate property=value`.

**steps** (required)
   An ordered array of build actions. Each element is a JSON object with
   exactly one key. The order of steps matters -- they are executed
   sequentially, and changing any step invalidates the cache for that
   step and all subsequent steps.

   ``{"run": "command"}``
      Execute a shell command inside the jail. The command is passed to
      :command:`/bin/sh -c` for execution. A ZFS snapshot is created
      after each successful ``run`` step.

      .. code-block:: json

         {"run": "pkg install -y nginx curl"}

      If the command exits with a non-zero status, the build fails.

   ``{"env": {"KEY": "VALUE", ...}}``
      Set environment variables. These are available to all subsequent
      ``run`` steps and are persisted in the resulting template for
      runtime use (see :ref:`Environment Variable Injection`).

      .. code-block:: json

         {"env": {"APP_PORT": "8080", "APP_ENV": "production"}}

      Multiple key-value pairs can be set in a single ``env`` step.
      An ``env`` step does not create a ZFS snapshot on its own, but
      it is included in the layer hash chain and is baked into the next
      ``run`` step's snapshot.

.. index:: Build Command
.. _Build Command:

The Build Command
-----------------

Build a template from a :file:`RapSheet.json`:

:samp:`# iocage build`

This reads :file:`RapSheet.json` from the current directory and produces
a template with an auto-generated name.

.. index:: Build Command Options
.. _Build Options:

Options
+++++++

``-f, --file PATH``
   Path to the RapSheet file. Defaults to :file:`RapSheet.json` in the
   current directory.

   :samp:`# iocage build -f /path/to/my-rapsheet.json`

``-n, --name NAME``
   Name for the resulting template.

   :samp:`# iocage build -n mywebserver`

``-t, --tag TAG``
   Apply a tag to the resulting template. Tags use the format
   ``name:version``.

   :samp:`# iocage build -n mywebserver -t mywebserver:v1.0`

``--no-cache``
   Ignore the layer cache and rebuild all steps from scratch.

   :samp:`# iocage build --no-cache`

``--keep-build-jail``
   On build failure, keep the temporary build jail for debugging instead
   of destroying it. The jail's name is printed so it can be inspected
   with :command:`iocage console`.

   :samp:`# iocage build --keep-build-jail`

**Additional properties** can be passed as arguments to configure the
temporary build jail (e.g. networking). These do not affect the resulting
template:

:samp:`# iocage build ip4_addr="vnet0|10.0.0.5/24"`

.. index:: Build Examples
.. _Build Examples:

Examples
++++++++

**Basic build:**

.. code-block:: none

   # iocage build -n mywebserver -t mywebserver:v1.0

**Create jails from the built template:**

.. code-block:: none

   # iocage create -t mywebserver -n web1 ip4_addr="vnet0|10.0.0.5/24"
   # iocage create -t mywebserver -n web2 ip4_addr="vnet0|10.0.0.6/24"
   # iocage start web1
   # iocage start web2

**Rebuild with a modified RapSheet (uses cache for unchanged steps):**

.. code-block:: none

   # iocage build -n mywebserver-v2 -t mywebserver:v2.0

.. index:: Build Networking
.. _Build Networking:

Build Jail Networking
+++++++++++++++++++++

During the build, a temporary jail is created to execute the ``run``
steps. This jail defaults to ``ip4=inherit``, which shares the host's
network stack. This allows ``run`` steps to access the network (e.g.
:command:`pkg install`) without additional configuration.

To override the default networking, pass properties as arguments:

:samp:`# iocage build ip4_addr="vnet0|10.0.0.50/24" defaultrouter="10.0.0.1"`

The build jail's networking configuration is not carried into the
resulting template. Network settings for jails created from the template
must be configured separately.

.. index:: Layer Caching
.. _Layer Caching:

Layer Caching
-------------

The build system uses ZFS snapshots to cache the result of each ``run``
step. On subsequent builds, unchanged steps are skipped by restoring
from the cached snapshot. This dramatically speeds up iterative
development where only later steps in a RapSheet are modified.

.. index:: Layer Hash Chain
.. _Layer Hash Chain:

How Layer Hashing Works
+++++++++++++++++++++++

Each step in the RapSheet is assigned a deterministic hash that depends
on **all preceding steps**. This forms a hash chain:

.. code-block:: none

   base_hash  = SHA256("from:15.0-RELEASE")
   step_0_hash = SHA256(base_hash + "|" + canonical_json(step_0))
   step_1_hash = SHA256(step_0_hash + "|" + canonical_json(step_1))
   step_2_hash = SHA256(step_1_hash + "|" + canonical_json(step_2))
   ...

Where ``canonical_json`` is the JSON serialization of the step with keys
sorted alphabetically. This ensures deterministic hashing regardless of
key order in the RapSheet.

**Key property:** Changing any step invalidates the hash of that step
and every subsequent step. This mirrors Docker's layer invalidation
behaviour.

The ``metadata`` and ``properties`` sections are **not** part of the
hash chain, as they do not affect the build layers. Changing metadata
or properties alone does not trigger a rebuild.

.. index:: Cache Hit
.. _Cache Hit:

Cache Hits and Misses
+++++++++++++++++++++

Before building, iocage scans the build cache for snapshots matching
each step's hash. The deepest consecutive match from step 0 determines
the cache hit point:

- **Full cache hit** (all steps matched): The build completes instantly
  by cloning from the final cached snapshot.
- **Partial cache hit** (steps 0-N matched): The build jail is created
  by cloning from the cached snapshot at step N, then executes steps
  N+1 onward.
- **No cache hit**: The build starts from scratch, creating a jail from
  the ``from`` source.

Cloning from a cached snapshot is instantaneous thanks to ZFS
copy-on-write semantics. No data is copied -- the clone shares all
blocks with the cached snapshot.

.. index:: Cache Management
.. _Cache Management:

Managing the Cache
++++++++++++++++++

**Bypass the cache** for a single build:

:samp:`# iocage build --no-cache`

**Clear the entire build cache:**

:samp:`# iocage clean --builds`

This destroys all cached layers under the ``builds/cache`` dataset.
Existing templates built from the cache are unaffected, as they are
promoted to be independent of the cache (see :ref:`ZFS Build Layout`).

.. index:: ZFS Build Layout
.. _ZFS Build Layout:

ZFS Build and Cache Layout
--------------------------

The build system uses dedicated ZFS datasets to store the cache and
manage the build process.

.. index:: Build Dataset Structure
.. _Build Dataset Structure:

Dataset Structure
+++++++++++++++++

.. code-block:: none

   pool/iocage/builds/                          # Build root dataset
   pool/iocage/builds/cache/                    # Cache container
   pool/iocage/builds/cache/{cache_id}/root     # Cached build chain
   pool/iocage/builds/cache/{cache_id}/root@{hash12}  # Layer snapshot

Within a single cache chain, layer snapshots are **sequential** on the
same dataset. Each snapshot stores only the blocks that changed since
the previous snapshot (the diff), making the cache highly space
efficient.

During a build, the temporary build jail lives under the standard jails
location:

.. code-block:: none

   pool/iocage/jails/{build_uuid}/root          # Temporary build jail
   pool/iocage/jails/{build_uuid}/root@{hash12} # Layer snapshots

.. index:: Build Process ZFS
.. _Build Process ZFS:

Build Process
+++++++++++++

The following describes the ZFS operations during a build:

1. **Cache lookup:** Scan snapshots under ``builds/cache/`` for
   matching layer hashes.

2. **Create build jail:**

   - *Cache hit at step N:* Clone from the cached snapshot:

     :samp:`zfs clone builds/cache/{cache_id}/root@{hash12} jails/{build_uuid}/root`

   - *No cache hit:* Create a new jail from the ``from`` source using
     the standard jail creation machinery.

3. **Execute uncached steps:** For each ``run`` step, execute the
   command inside the jail, then create a snapshot:

   :samp:`zfs snapshot jails/{build_uuid}/root@{hash12}`

   The full layer hash is stored as a ZFS user property on the
   snapshot:

   :samp:`zfs set org.freebsd.iocage:layer_hash={hash} jails/{build_uuid}/root@{hash12}`

4. **Store in cache:** After all steps complete, the build jail dataset
   is renamed into the cache:

   :samp:`zfs rename jails/{build_uuid} builds/cache/{new_id}`

5. **Create template:** Clone the final layer snapshot to create the
   template:

   :samp:`zfs clone builds/cache/{id}/root@{final_hash} templates/{name}/root`

6. **Promote template:** Promote the template to make it independent
   of the cache:

   :samp:`zfs promote templates/{name}/root`

   After promotion, the template owns its data. The cache entry becomes
   a lightweight clone of the template. If the cache is later cleaned,
   the template is unaffected.

.. index:: Build Space Efficiency
.. _Build Space Efficiency:

Space Efficiency
++++++++++++++++

The cache design leverages two ZFS features for space efficiency:

**Sequential snapshots:**
   Within a build chain, each snapshot stores only the changed blocks
   (the delta). For example, if a 2 GB base system has
   :command:`pkg install nginx` run on it (adding ~50 MB), the snapshot
   consumes approximately 50 MB, not 2 GB.

**Copy-on-write clones:**
   When a build creates a clone from a cached snapshot (cache hit), the
   clone initially consumes zero additional space. Only blocks that are
   subsequently modified consume new space.

.. index:: Environment Variables
.. _Environment Variable Injection:

Environment Variable Injection
------------------------------

Environment variables set with ``env`` steps in the RapSheet are
persisted in the resulting template and injected at multiple entry
points to ensure broad availability.

.. index:: Build-time Environment
.. _Build-time Environment:

Build Time
++++++++++

During the build, environment variables from ``env`` steps are passed
to subsequent ``run`` steps via the subprocess environment. The
``run`` command inherits these variables through :command:`jexec`.

.. index:: Runtime Environment
.. _Runtime Environment:

Runtime
+++++++

Environment variables are stored in the template's :file:`config.json`
as the ``build_env`` property (a JSON-encoded dictionary). They are
injected at the following entry points:

.. table:: **Environment Variable Injection Points**
   :class: longtable

   +------------------------+---------------------------+---------------------------------------------+
   | Entry Point            | Mechanism                 | Coverage                                    |
   +========================+===========================+=============================================+
   | Jail startup           | Prepended to              | PID 1 and all child processes (services      |
   | (:command:`exec_start`)| :command:`exec_start` as  | started by :command:`/bin/sh /etc/rc`)       |
   |                        | :command:`env KEY=VAL`    | inherit the variables.                      |
   +------------------------+---------------------------+---------------------------------------------+
   | :command:`iocage exec` | Merged into the           | Commands run via :command:`iocage exec`      |
   |                        | subprocess environment    | see the variables.                          |
   |                        | (:command:`su_env`)       |                                             |
   +------------------------+---------------------------+---------------------------------------------+
   | Login sessions         | Written to                | SSH sessions, :command:`iocage console`,    |
   | (SSH, console)         | :file:`/etc/login.conf`   | and :command:`su -l` sessions see the       |
   |                        | (``setenv`` capability)   | variables.                                  |
   +------------------------+---------------------------+---------------------------------------------+

.. index:: login.conf Environment
.. _login.conf Environment:

login.conf Integration
++++++++++++++++++++++

During the build, environment variables are written to the jail's
:file:`/etc/login.conf` using the ``setenv`` capability of the default
login class:

.. code-block:: none

   default:\
       ...existing capabilities...\
       :setenv=HTTP_PORT=80,APP_ENV=production:\

The login database is rebuilt with :command:`cap_mkdb /etc/login.conf`
after modification. This ensures environment variables are available for
all login-based sessions (SSH, console, :command:`su -l`).

.. note:: Direct :command:`jexec` invocations from outside iocage bypass
   the login process. For these, only the ``login.conf`` mechanism
   applies (for login shells). Use :command:`iocage exec` for full
   environment variable support.

.. index:: Overriding Environment
.. _Overriding Environment:

Overriding Environment Variables
++++++++++++++++++++++++++++++++

Environment variables set during the build can be viewed with:

:samp:`# iocage get build_env [TEMPLATE|JAIL]`

To override at jail creation time, modify the ``build_env`` property
after creating the jail:

:samp:`# iocage set build_env='{"APP_ENV":"staging"}' [UUID|NAME]`

.. index:: Tagging
.. _Tagging:

Tagging Templates
-----------------

Tags provide version labels for templates, similar to Docker image tags.
A tag has the format ``name:version`` (e.g. ``myapp:v1.0``,
``myapp:latest``).

.. index:: Tag Command
.. _Tag Command:

The Tag Command
+++++++++++++++

**Add a tag:**

:samp:`# iocage tag mytemplate myapp:v1.0`

**List tags:**

:samp:`# iocage tag mytemplate --list`

**Remove a tag:**

:samp:`# iocage tag mytemplate --remove myapp:v1.0`

.. index:: Tagging During Build
.. _Tagging During Build:

Tagging During Build
++++++++++++++++++++

Tags can be applied directly during the build with the ``-t`` option:

:samp:`# iocage build -n mywebserver -t mywebserver:v1.0`

A template can have multiple tags:

.. code-block:: none

   # iocage tag mywebserver mywebserver:latest
   # iocage tag mywebserver --list
   mywebserver:v1.0
   mywebserver:latest

Tags are stored in the template's :file:`config.json` as the ``tags``
property and as a ZFS user property
(``org.freebsd.iocage:tags``). Templates can be referenced by
their tags in any iocage command that accepts a jail or template name.

.. index:: Build Workflow
.. _Build Workflow:

Complete Build Workflow Example
-------------------------------

This example demonstrates a full workflow from creating a RapSheet to
deploying jails from the built template.

**1. Fetch a release (if not already available):**

.. code-block:: none

   # iocage fetch -r 15.0-RELEASE

**2. Create a RapSheet.json:**

.. code-block:: json

   {
       "from": "15.0-RELEASE",
       "metadata": {
           "maintainer": "Admin <admin@example.com>",
           "description": "Production Nginx web server",
           "labels": {
               "app": "nginx",
               "team": "platform"
           }
       },
       "properties": {
           "boot": 1,
           "allow_raw_sockets": 1
       },
       "steps": [
           {"env": {"APP_PORT": "8080", "APP_ENV": "production"}},
           {"run": "pkg install -y nginx"},
           {"run": "sysrc nginx_enable=YES"},
           {"run": "mkdir -p /usr/local/www/data"},
           {"run": "echo 'server { listen 8080; root /usr/local/www/data; }' > /usr/local/etc/nginx/nginx.conf"}
       ]
   }

**3. Build the template:**

.. code-block:: none

   # iocage build -n webserver -t webserver:v1.0
   Step 1/5: env APP_PORT=8080 APP_ENV=production
   Step 2/5: run pkg install -y nginx
   Step 3/5: run sysrc nginx_enable=YES
   Step 4/5: run mkdir -p /usr/local/www/data
   Step 5/5: run echo 'server { ... }' > /usr/local/etc/nginx/nginx.conf
   Template created: webserver (webserver:v1.0)

**4. Verify the template:**

.. code-block:: none

   # iocage list -t
   +----------+---------+-------+----------+------+
   | NAME     | RELEASE | STATE | TEMPLATE | TAG  |
   +==========+=========+=======+==========+======+
   | webserver| 15.0-R  | down  | yes      | v1.0 |
   +----------+---------+-------+----------+------+

**5. Create and start jails from the template:**

.. code-block:: none

   # iocage create -t webserver -n web1 ip4_addr="vnet0|10.0.0.10/24"
   # iocage create -t webserver -n web2 ip4_addr="vnet0|10.0.0.11/24"
   # iocage start web1
   # iocage start web2

**6. Verify environment variables:**

.. code-block:: none

   # iocage exec web1 echo $APP_PORT
   8080

**7. Iterate -- modify the RapSheet and rebuild:**

If only the last step changes, the build reuses cached layers for all
preceding steps:

.. code-block:: none

   # iocage build -n webserver-v2 -t webserver:v2.0
   Step 1/5: env APP_PORT=8080 APP_ENV=production [cached]
   Step 2/5: run pkg install -y nginx [cached]
   Step 3/5: run sysrc nginx_enable=YES [cached]
   Step 4/5: run mkdir -p /usr/local/www/data [cached]
   Step 5/5: run echo 'updated config...' > /usr/local/etc/nginx/nginx.conf
   Template created: webserver-v2 (webserver:v2.0)

.. index:: Build Error Handling
.. _Build Error Handling:

Error Handling
--------------

If a ``run`` step fails (exits with a non-zero status), the build
stops immediately. By default, the temporary build jail is destroyed.

To keep the build jail for debugging, use the ``--keep-build-jail``
option:

.. code-block:: none

   # iocage build --keep-build-jail
   Step 1/3: run pkg install -y nginx
   Step 2/3: run bad-command
   Build failed at step 2: bad-command (exit code 127)
   Build jail retained: ioc-build-a1b2c3d4
   Debug with: iocage console ioc-build-a1b2c3d4

After debugging, destroy the build jail manually:

:samp:`# iocage destroy ioc-build-a1b2c3d4`
