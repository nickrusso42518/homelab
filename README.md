# Home Lab Validator
Many engineers with personally-owned home labs are looking for a cost
effective way to perform automated validation of their environment,
especially if the equipment is not commonly used. This project is especially
designed for old hardware that does not support modern APIs or integration
with monitoring systems.

> Contact information:\
> Email:    njrusmc@gmail.com\
> Twitter:  @nickrusso42518

  * [Tested Platforms](#tested-platforms)
  * [Usage](#usage)
  * [Log Files](#log-files)

## Tested Platforms
Testing was conducted on the following platforms and versions:
  * Cisco Catalyst 3750X, version 15.2(4)E10
  * VMware ESXI, version 6.0 Update 2 Build 3620759
  * Synology RS-814, version 6.2.2

The code is likely to work on these products running different versions
without any problems as only SSH/CLI is used; no modern API interactions.

## Usage
First, install the required Python packages. I'd recommend using Python 3.10
or newer, but Python 3.7 will likely also work: `pip install -r requirements.txt`

You can also use `pip install -r requirements.freeze.txt` to exactly
match my versions, but you may miss out on package updates.

This is a "some assembly required" project. Next, create `config.json` and
populate it with your connectivity parameters. An example is included in the
`samples/` directory. This project assumes that all of your lab devices share
the same username and password for consistency. If that's not true, you'll
have to find the `auth_username` and `auth_password` for each host individually,
while removing it from the `login` dictionary.

Last, you should open the `validate.py` script and carefully review the
different test cases. Be sure to change the version, serial number, VLAN,
and other strings that are specific to your environment. I strongly encourage
you to add, delete, and modify various tests. The existing script is very
specific to my environment and the aspects important to me. This will differ
for each user.

## Log Files
If you find the test-and-assert process too difficult at first, you can simply
send commands to the devices as a first step. Each supported platform has a
nested `_send_cmd_*` coroutine that simplifies the communication process,
automatically parsing the output (if possible), and writing the results to
a host-specific log file in human-readable form. You can quickly and reliably
collect data from your device, review the log files, then determine which
fields are worth evaluating. Log files from my personal environment are
included in the `samples/` directory.
