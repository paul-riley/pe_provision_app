# PE Provision Application

A single-file Python/Tkinter desktop application for provisioning a Puppet Agent on an Ubuntu machine running GNOME. It collects a serial number, store number, and timezone, writes the Puppet configuration, then downloads and runs the Puppet Agent installer from `pe.caffeine.lan`.

The window is titled **PE Provision Application** and uses an embedded custom icon instead of the default Tk icon. GNOME controls whether an icon appears in the title bar.

## Requirements

- Ubuntu with a GNOME desktop session.
- Python 3, Tkinter, and PyYAML.
- `curl`, Bash, and `pkexec` with a working desktop authentication agent.
- An account authorized to authenticate as an administrator.
- DNS resolution and network access to `pe.caffeine.lan`, including port `8140` for the installer.
- A Puppet Enterprise installer that supports the target operating system.

The requested target is Ubuntu 26.06 with GNOME. The GUI and end-to-end installation have not yet been verified on that target.

## Install and launch

Install the dependencies:

```bash
sudo apt update
sudo apt install python3-tk python3-yaml curl policykit-1
```

If your Ubuntu release does not provide `policykit-1`, install its `pkexec` package instead. Confirm that `pkexec` is available before provisioning.

Copy `pe_provision_app.py` to the target machine, open a terminal in its directory, and launch it as your normal desktop user:

```bash
python3 pe_provision_app.py
```

The application code is contained in this one Python file; no separate images or configuration templates are required. Its Python dependencies must also be available to the system interpreter used by the privileged helper.

## Use the application

1. Enter the **Serial Number**.
2. Enter the **Store Number**.
3. Select a **Timezone**.
4. Check the displayed certname and click **Provision and Install**.
5. Complete the GNOME administrator authentication prompt.
6. Watch the log panel for progress and the completion or error dialog.

Keep the application open until provisioning finishes. Certificate signing may still be required after the installer completes.

### Input rules

| Field | Accepted values | Result |
| --- | --- | --- |
| Serial Number | 1–63 ASCII letters or digits | Lowercased and suffixed with `.caffeine.lan` to form the certname |
| Store Number | An integer from `1` through `1000000` | Stored as a string in a certificate extension; leading zeros are removed |
| Timezone | Eastern, Central, Mountain, Pacific, Alaskan, or Hawaiian | The selected label is stored in a certificate extension |

For example, serial number `ABC123`, store number `42`, and timezone `Mountain` produce certname `abc123.caffeine.lan`.

The timezone selection supplies Puppet metadata. It does not change the machine's operating-system timezone.

## Provisioning behavior

The application performs these steps in an administrator helper process while the GUI remains responsive:

1. Validates the inputs and checks that required dependencies are available.
2. Checks for an existing Puppet certificate, CSR, or private key. If found, provisioning stops before writing configuration.
3. Builds the Puppet configuration and CSR extension requests.
4. Backs up existing configuration files beside the originals using a `.bak-<timestamp>` suffix.
5. Writes the configuration files.
6. Downloads the installer into a temporary directory and runs it with Bash as root.

### Puppet configuration

The application creates or updates `/etc/puppetlabs/puppet/puppet.conf`:

```ini
[main]
certname = abc123.caffeine.lan
csr_attributes = /etc/puppetlabs/puppet/csr_attributes.yaml
```

Other settings are preserved, but existing `certname` and `csr_attributes` settings are removed from other sections so they cannot override these values. Rewriting the file does not preserve comments or original formatting.

The application does not explicitly set the Puppet `server` setting; it preserves an existing value and runs the installer hosted by `pe.caffeine.lan`.

It also creates or updates `/etc/puppetlabs/puppet/csr_attributes.yaml`:

```yaml
extension_requests:
  1.3.6.1.4.1.34380.1.2.1: '42'
  1.3.6.1.4.1.34380.1.2.2: Mountain
```

Existing unrelated YAML data is preserved, including other extension requests and custom attributes. Comments and original formatting are not preserved. The application refuses to replace either configuration file if it is a symbolic link.

### Agent installer

The requested installation command is:

```bash
curl -k https://pe.caffeine.lan:8140/packages/current/install.bash | sudo bash
```

The application implements this as a separate download followed by execution. It uses `curl -f -k -sS -L`, a 30-second connection timeout, and a 600-second overall download timeout. Bash runs only after a successful, nonempty download. The application passes the serial-based certname explicitly as both `main:certname` and `agent:certname`, along with the CSR path and both extension requests, so the installer receives them before its initial Puppet run. It checks the effective agent certname after installation. The helper already runs as root, so it does not invoke `sudo` again.

The `-k` option disables TLS certificate verification, matching the requested command. Installer output appears in the GUI log. The temporary download is removed when the helper exits normally, including handled failures.

## Configure trusted facts on Puppet Server

Store number and timezone are requested certificate extensions. They become trusted facts when the CA signs the agent's certificate with those extensions included.

The application uses these site-specific private OIDs:

| OID | Intended short name | Example value |
| --- | --- | --- |
| `1.3.6.1.4.1.34380.1.2.1` | `store_number` | `'42'` |
| `1.3.6.1.4.1.34380.1.2.2` | `timezone` | `'Mountain'` |

Ensure these OIDs are not already assigned to different metadata in your Puppet deployment.

To access the extensions by the short names above, merge the following into `$confdir/custom_trusted_oid_mapping.yaml` on **each Puppet Server infrastructure node**, including catalog compilers. The usual location is `/etc/puppetlabs/puppet/custom_trusted_oid_mapping.yaml`.

```yaml
oid_mapping:
  1.3.6.1.4.1.34380.1.2.1:
    shortname: store_number
    longname: Store Number
  1.3.6.1.4.1.34380.1.2.2:
    shortname: timezone
    longname: Timezone
```

Preserve any existing mappings and restart `puppetserver` on the affected servers after updating the file:

```bash
sudo systemctl restart puppetserver
```

Puppet manifests can then read:

```puppet
$store_number = $trusted['extensions']['store_number']
$timezone = $trusted['extensions']['timezone']
```

Without the server-side mapping, use the numeric OIDs:

```puppet
$store_number = $trusted['extensions']['1.3.6.1.4.1.34380.1.2.1']
$timezone = $trusted['extensions']['1.3.6.1.4.1.34380.1.2.2']
```

Both extension values are strings. The application does not sign certificates or configure CA autosigning.

See Puppet's documentation for [certificate extension requests](https://help.puppet.com/core/current/Content/PuppetCore/csr_extension_requests.htm) and [custom trusted OID mappings](https://help.puppet.com/core/current/content/puppetcore/config_file_oid_map.htm).

## Troubleshooting and recovery

| Symptom | Action |
| --- | --- |
| `No module named tkinter` or `yaml` | Install `python3-tk` and `python3-yaml` for the system Python interpreter. |
| No administrator prompt or missing `pkexec` | Check that `pkexec` is installed and that the app was launched in an active GNOME desktop session with an authentication agent. |
| `Existing Puppet identity detected` | Have a Puppet administrator retire the previous identity and handle its local SSL material before reprovisioning. The app never deletes certificates, CSRs, or private keys. |
| Download fails | Check DNS, network access to `pe.caffeine.lan:8140`, and availability of the installer endpoint. |
| Installer fails | Read the GUI log and resolve the reported installer error. Configuration files remain in place; they are not automatically rolled back. |
| Trusted facts are absent | Confirm the certificate was signed with the requested extensions. For named keys, check the OID mapping on every relevant Puppet Server and restart `puppetserver`. |

Certificate extensions are fixed when the certificate is signed. Changing store or timezone metadata on an enrolled machine requires an administrator-managed certificate replacement process.

Backups are stored alongside the configuration files, for example:

```text
/etc/puppetlabs/puppet/puppet.conf.bak-<timestamp>
/etc/puppetlabs/puppet/csr_attributes.yaml.bak-<timestamp>
```

An administrator can restore the appropriate backups if needed. Newly created files have no previous version to restore. File replacement is atomic for each file individually; the two configuration writes and agent installation are not one transaction.

## Validation status

Python compilation and checks for input validation, preservation of unrelated configuration data, extension values, and backup creation have passed. The GNOME interface, privileged authentication flow, and real Puppet Agent installation require validation on the target Ubuntu machine.
