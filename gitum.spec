Name: gitum
Version: 0.5.0
Release: alt1

Summary: Git Upstream Manager
License: GPLv2
Group: Development/Other

# git clone git://git.etersoft.ru/people/piastry/packages/gitum.git

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
* Wed Mar 14 2012 Pavel Shilovsky <piastry@altlinux.org> 0.5.0-alt1
- Fix push command
- Fix clone command in the no config branch case
- Reorder parameters in create call
- Change default name for current branch to mainline
- Let gitum work with default branches if a config is missed
- Eliminate editpatch command
- Change restore command
- Save config file in an empty branch
- Use specified message of update command in patches branch
- Simplify update command
- Allow optional restore for rebased branch only
- Work in rebased branch rather than current
- Make rebased branch local only
- Fix bugs in pull
- Minor code style fixes

* Wed Feb 15 2012 Pavel Shilovsky <piastry@altlinux.org> 0.4.2-alt1
- Fix the argument parsing bug

* Fri Feb 03 2012 Pavel Shilovsky <piastry@altlinux.org> 0.4.1-alt1
- Push rebased branch with --force option
- Add --remote option for pull

* Fri Feb 03 2012 Pavel Shilovsky <piastry@altlinux.org> 0.4-alt1
- Add restore command
- Rename pull to merge
- Add clone/pull/push commands for a remote work
- Add --repo command line option to specify the repo path
- Bugfixes

* Wed Dec 14 2011 Pavel Shilovsky <piastry@altlinux.org> 0.3-alt1
- Make update command interface smarter
- Add editpatch command
- Fix a possible error during saving a state
- Improve logging

* Wed Nov 30 2011 Pavel Shilovsky <piastry@altlinux.org> 0.2-alt2
- Change a version in setup

* Wed Nov 30 2011 Pavel Shilovsky <piastry@altlinux.org> 0.2-alt1
- Improve create command
- Add remove command
- Simplify output messages
- Add branch argument to pull command
- Move config files to .git
- Use original authors for commits

* Mon Oct 31 2011 Pavel Shilovsky <piastry@altlinux.org> 0.1-alt1
- Initial build
