Name: gitum
Version: 0.2
Release: alt1

Summary: Git Upstream Manager
License: GPLv2
Group: Development/Other

# git clone git://git.etersoft.ru/people/piastry/packages/git-um.git

Url: http://www.etersoft.ru
Packager: Pavel Shilovsky <piastry@etersoft.ru>

Source: %name-%version.tar

BuildArch: noarch

BuildRequires: python-module-setuptools

Requires: git-core >= 1.7
Requires: python-module-GitPython = 0.3.0

Provides: gitum

Obsoletes: git-um

%description
Git Upstream Manager is the development tool that maintains your current
working git repository: pull upstream changes in the appropriate order
into your current working branch and keep all your patches up-to-date
and ready for a submission in the same time.

%prep
%setup -q

%build
%python_build

%install
%python_install

%files
%python_sitelibdir/*
%_bindir/%name

%changelog
* Wed Nov 30 2011 Pavel Shilovsky <piastry@altlinux.org> 0.2-alt1
- Improve create command
- Add remove command
- Simplify output messages
- Add branch argument to pull command
- Move config files to .git
- Use original authors for commits

* Mon Oct 31 2011 Pavel Shilovsky <piastry@altlinux.org> 0.1-alt1
- Initial build
