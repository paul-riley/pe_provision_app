#!/usr/bin/env python3
"""PE Provision Application — Ubuntu/GNOME.

Install prerequisites: sudo apt install python3-tk python3-yaml curl policykit-1
Run as your desktop user: python3 pe_provision_app.py
GNOME prompts for administrator authentication when Provision is clicked.

Trusted certificate extensions (values are strings):
  store_number: 1.3.6.1.4.1.34380.1.2.1
  timezone:     1.3.6.1.4.1.34380.1.2.2
  pp_role:      pos_terminal (registered Puppet extension; no custom mapping)
Ordinary external fact: register_number (integer 1, 2, or 3), written to
/etc/puppetlabs/facter/facts.d/pe_provision.yaml before the installer runs.
On EACH Puppet Server, merge this into custom_trusted_oid_mapping.yaml:
  oid_mapping:
    1.3.6.1.4.1.34380.1.2.1:
      shortname: store_number
      longname: Store Number
    1.3.6.1.4.1.34380.1.2.2:
      shortname: timezone
      longname: Timezone
Restart puppetserver after configuring the mapping. Access these with
$trusted['extensions']['store_number'] and $trusted['extensions']['timezone'].
Without the mapping, access them by numeric OID. Extensions become trusted
when the CA signs the CSR. Existing certificates/CSRs must be retired by an
administrator before reprovisioning; this app never deletes SSL material.
Timezone stores the selected label; it does not change the OS timezone.
"""

import argparse
import configparser
import datetime
import os
from pathlib import Path
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading

TITLE = 'PE Provision Application'
TIMEZONES = ('Eastern', 'Central', 'Mountain', 'Pacific', 'Alaskan', 'Hawaiian')
STORE_OID = '1.3.6.1.4.1.34380.1.2.1'
TIMEZONE_OID = '1.3.6.1.4.1.34380.1.2.2'
REGISTERS = ('1', '2', '3')
ROLE = 'pos_terminal'
FACTS_PATH = Path('/etc/puppetlabs/facter/facts.d/pe_provision.yaml')
CONF_DIR = Path('/etc/puppetlabs/puppet')
INSTALL_URL = 'https://pe.caffeine.lan:8140/packages/current/install.bash'


def validate(serial, store, timezone, register):
    if not re.fullmatch(r'[A-Za-z0-9]{1,63}', serial):
        raise ValueError('Serial Number must contain 1–63 ASCII letters or numbers.')
    if not re.fullmatch(r'[0-9]+', store) or not 1 <= int(store) <= 1000000:
        raise ValueError('Store Number must be an integer from 1 to 1000000.')
    if timezone not in TIMEZONES:
        raise ValueError('Select a Timezone from the list.')
    if register not in REGISTERS:
        raise ValueError('Select a Register Number of 1, 2, or 3.')
    return serial.lower() + '.caffeine.lan', str(int(store)), timezone


def configuration(serial, store, timezone, register, directory, facts_path=FACTS_PATH):
    """Build configuration and external facts before writing any files; preserve other config settings."""
    import yaml
    certname, store, timezone = validate(serial, store, timezone, register)
    config = configparser.ConfigParser(interpolation=None, strict=True)
    config.optionxform = str
    path = directory / 'puppet.conf'
    if path.exists():
        config.read_string(path.read_text())
    if not config.has_section('main'):
        config.add_section('main')
    # Remove overrides so the configured certname and CSR path are effective.
    for section in config.sections():
        for key in list(config[section]):
            if key.lower() in ('certname', 'csr_attributes'):
                config.remove_option(section, key)
    config['main']['certname'] = certname
    config['main']['csr_attributes'] = str(directory / 'csr_attributes.yaml')
    import io
    output = io.StringIO()
    config.write(output)
    csr_path = directory / 'csr_attributes.yaml'
    data = yaml.safe_load(csr_path.read_text()) if csr_path.exists() else {}
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ValueError('Existing csr_attributes.yaml must contain a YAML mapping.')
    extensions = data.setdefault('extension_requests', {})
    if not isinstance(extensions, dict):
        raise ValueError('Existing extension_requests must be a YAML mapping.')
    extensions[STORE_OID] = store
    extensions[TIMEZONE_OID] = timezone
    extensions.pop('1.3.6.1.4.1.34380.1.1.13', None)
    extensions['pp_role'] = ROLE
    facts = yaml.safe_load(facts_path.read_text()) if facts_path.exists() else {}
    if facts is None:
        facts = {}
    if not isinstance(facts, dict):
        raise ValueError('Existing external facts file must contain a YAML mapping.')
    facts['register_number'] = int(register)
    return {path: output.getvalue(), csr_path: yaml.safe_dump(data, sort_keys=False),
            facts_path: yaml.safe_dump(facts, sort_keys=False)}


def write_configuration(files):
    stamp = datetime.datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    for path in files:
        if path.is_symlink():
            raise ValueError(f'Refusing to replace symlink: {path}')
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            backup = path.with_name(path.name + '.bak-' + stamp)
            shutil.copy2(path, backup)
            print(f'Backup: {backup}', flush=True)
    for path, content in files.items():
        fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.' + path.name)
        try:
            with os.fdopen(fd, 'w') as stream:
                stream.write(content)
            os.chmod(temporary, 0o644)
            os.replace(temporary, path)
            print(f'Created {path}', flush=True)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


def installer_command(installer, serial, store, timezone, register):
    """Give the installer the identity before its initial Puppet run."""
    certname, store, timezone = validate(serial, store, timezone, register)
    return ['bash', str(installer),
            f'main:certname={certname}',
            f'agent:certname={certname}',
            f'main:csr_attributes={CONF_DIR / "csr_attributes.yaml"}',
            f'extension_requests:{STORE_OID}={store}',
            f'extension_requests:{TIMEZONE_OID}={timezone}',
            f'extension_requests:pp_role={ROLE}']


def provision(serial, store, timezone, register):
    if os.geteuid() != 0:
        raise PermissionError('Provisioning requires administrator privileges.')
    validate(serial, store, timezone, register)
    import yaml  # Verify dependency before changing files.
    for command in ('curl', 'bash'):
        if not shutil.which(command):
            raise RuntimeError(f'Required command is missing: {command}')
    # Check the active SSL directory too, if an agent is already installed.
    ssl_dirs = {CONF_DIR / 'ssl'}
    puppet = Path('/opt/puppetlabs/bin/puppet')
    if puppet.exists():
        result = subprocess.run([str(puppet), 'config', 'print', 'ssldir', '--section', 'agent'],
                                check=True, capture_output=True, text=True, timeout=30)
        ssl_dirs.add(Path(result.stdout.strip()))
    for ssl in ssl_dirs:
        for subdir in ('certs', 'certificate_requests', 'private_keys'):
            if any(p.name != 'ca.pem' for p in (ssl / subdir).glob('*.pem')):
                raise RuntimeError('Existing Puppet identity detected. Have an administrator '
                                   'retire its certificate/CSR before provisioning again.')
    files = configuration(serial, store, timezone, register, CONF_DIR)
    write_configuration(files)
    # Equivalent to curl -k URL | sudo bash, but a failed download cannot
    # execute a partial installer. This helper is already running as root.
    with tempfile.TemporaryDirectory(prefix='pe-provision-') as temporary:
        installer = Path(temporary) / 'install.bash'
        print('Downloading Puppet Agent installer…', flush=True)
        subprocess.run(['curl', '-f', '-k', '-sS', '-L', '--connect-timeout', '30',
                        '--max-time', '600', INSTALL_URL, '-o', str(installer)], check=True)
        if not installer.stat().st_size:
            raise RuntimeError('The downloaded installer is empty.')
        print(f'Running Puppet Agent installer for {validate(serial, store, timezone, register)[0]}…', flush=True)
        subprocess.run(installer_command(installer, serial, store, timezone, register),
                       check=True, stdin=subprocess.DEVNULL)
    result = subprocess.run([str(puppet), 'config', 'print', 'certname', '--section', 'agent'],
                            check=True, capture_output=True, text=True, timeout=30)
    expected = validate(serial, store, timezone, register)[0]
    if result.stdout.strip() != expected:
        raise RuntimeError(f'Installed agent certname is {result.stdout.strip()!r}; '
                           f'expected {expected!r}. Stop the agent and inspect its configuration.')
    print('Installer completed. Trusted facts are available after CA signing.', flush=True)


def launch_gui():
    import tkinter as tk
    from tkinter import ttk, messagebox

    root = tk.Tk()
    root.title(TITLE)
    root.geometry('640x640')
    root.minsize(560, 480)
    # Replace Tk's default window icon. GNOME may omit title-bar icons entirely.
    icon = tk.PhotoImage(width=32, height=32)
    icon.put('#244b64', to=(0, 0, 32, 32))
    icon.put('#ffffff', to=(8, 7, 12, 25))
    icon.put('#ffffff', to=(12, 7, 24, 11))
    icon.put('#ffffff', to=(12, 14, 22, 18))
    root.iconphoto(True, icon)
    frame = ttk.Frame(root, padding=24)
    frame.pack(fill='both', expand=True)
    frame.columnconfigure(1, weight=1)
    ttk.Label(frame, text=TITLE, font=('', 18, 'bold')).grid(
        row=0, column=0, columnspan=2, sticky='w', pady=(0, 22))
    serial = tk.StringVar()
    store = tk.StringVar()
    timezone = tk.StringVar()
    register = tk.StringVar()
    serial_check = (root.register(lambda s: re.fullmatch(r'[A-Za-z0-9]{0,63}', s) is not None), '%P')
    store_check = (root.register(lambda s: not s or (re.fullmatch(r'[0-9]{1,7}', s) is not None
                                                   and 1 <= int(s) <= 1000000)), '%P')
    controls = []
    for row, (label, variable, check) in enumerate(
            [('Serial Number', serial, serial_check), ('Store Number', store, store_check)], 1):
        ttk.Label(frame, text=label).grid(row=row, column=0, sticky='w', padx=(0, 20), pady=8)
        entry = ttk.Entry(frame, textvariable=variable, validate='key', validatecommand=check)
        entry.grid(row=row, column=1, sticky='ew', pady=8)
        controls.append(entry)
    ttk.Label(frame, text='Timezone').grid(row=3, column=0, sticky='w', pady=8)
    combo = ttk.Combobox(frame, textvariable=timezone, values=TIMEZONES, state='readonly')
    combo.grid(row=3, column=1, sticky='ew', pady=8)
    ttk.Label(frame, text='Register Number').grid(row=4, column=0, sticky='w', pady=8)
    register_combo = ttk.Combobox(frame, textvariable=register, values=REGISTERS, state='readonly')
    register_combo.grid(row=4, column=1, sticky='ew', pady=8)
    ttk.Label(frame, text='Role').grid(row=5, column=0, sticky='w', pady=8)
    ttk.Label(frame, text=ROLE).grid(row=5, column=1, sticky='w', pady=8)
    preview = tk.StringVar(value='Certname: —')
    serial.trace_add('write', lambda *_: preview.set(
        'Certname: ' + (serial.get().lower() + '.caffeine.lan' if serial.get() else '—')))
    ttk.Label(frame, textvariable=preview).grid(row=6, column=0, columnspan=2, sticky='w', pady=10)
    ttk.Label(frame, text='Creates Puppet configuration, then installs Puppet Agent.\n'
              'Administrator authentication is required.', wraplength=540).grid(
                  row=7, column=0, columnspan=2, sticky='w', pady=(0, 12))
    messages = queue.Queue()
    busy = False
    log = tk.Text(frame, height=9, wrap='word', state='disabled')
    log.grid(row=9, column=0, columnspan=2, sticky='nsew', pady=(12, 0))
    frame.rowconfigure(9, weight=1)

    def worker(values):
        try:
            command = [sys.executable, str(Path(__file__).resolve()), '--provision', *values]
            if os.geteuid() != 0:
                command = ['pkexec', *command]
            process = subprocess.Popen(command, stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT, text=True, bufsize=1)
            for line in process.stdout:
                messages.put(('log', line))
            code = process.wait()
            messages.put(('done', code))
        except Exception as exc:
            messages.put(('log', str(exc) + '\n'))
            messages.put(('done', -1))

    def start():
        nonlocal busy
        values = (serial.get(), store.get(), timezone.get(), register.get())
        try:
            validate(*values)
        except ValueError as exc:
            messagebox.showerror(TITLE, str(exc), parent=root)
            return
        busy = True
        for widget in [*controls, combo, register_combo, button]:
            widget.configure(state='disabled')
        messages.put(('log', '\nStarting provisioning…\n'))
        threading.Thread(target=worker, args=(values,), daemon=True).start()

    button = ttk.Button(frame, text='Provision and Install', command=start)
    button.grid(row=8, column=0, columnspan=2, sticky='ew')

    def poll():
        nonlocal busy
        while not messages.empty():
            kind, value = messages.get_nowait()
            if kind == 'log':
                log.configure(state='normal')
                log.insert('end', value)
                log.see('end')
                log.configure(state='disabled')
            else:
                busy = False
                for widget in [*controls, button]:
                    widget.configure(state='normal')
                combo.configure(state='readonly')
                register_combo.configure(state='readonly')
                if value == 0:
                    messagebox.showinfo(TITLE, 'Puppet Agent installation completed.\n'
                                        'Certificate signing may still be required.', parent=root)
                else:
                    messagebox.showerror(TITLE, 'Provisioning did not complete. See the log.\n'
                                         'Configuration backups are kept beside the originals.', parent=root)
        root.after(100, poll)

    def close():
        if busy:
            messagebox.showinfo(TITLE, 'Wait for provisioning to finish before closing.', parent=root)
        else:
            root.destroy()

    root.protocol('WM_DELETE_WINDOW', close)
    controls[0].focus_set()
    poll()
    root.mainloop()


def main():
    parser = argparse.ArgumentParser(description=TITLE, epilog=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--provision', nargs=4, metavar=('SERIAL', 'STORE', 'TIMEZONE', 'REGISTER'),
                        help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.provision:
        try:
            provision(*args.provision)
        except Exception as exc:
            print(f'Provisioning failed: {exc}', file=sys.stderr, flush=True)
            return 1
    else:
        launch_gui()
    return 0


if __name__ == '__main__':
    sys.exit(main())
