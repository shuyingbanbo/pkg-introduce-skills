Name:           ROS_PACKAGE_NAME
Version:        ROS_PACKAGE_VERSION
Release:        ROS_PACKAGE_RELEASE%{?dist}
Summary:        QuantStack tools library - xtensor dependency
License:        BSD-3-Clause
URL:            https://github.com/xtensor-stack/xtl

Source0:        %{name}-%{version}.tar.gz

BuildRequires:  cmake
BuildRequires:  gcc-c++

# Header-only library - no debug package needed
%global debug_package %{nil}

%description
The xtl library is a set of general purpose tools for C++ used by
the xtensor stack. It includes:
- Optional and nullable types
- Basic fixed string
- Complex numbers utilities
- JSON and base64 support

%package devel
Summary:        %{summary}
Provides:       %{name} = %{version}-%{release}
Provides:       %{name}-static = %{version}-%{release}
BuildArch:      noarch

%description devel
Development files for xtl library. This is a header-only library.

%prep
%autosetup -n %{name}-%{version}

%build
%cmake -DBUILD_TESTS=OFF

%install
%make_install

%files devel
%doc README.md
%license LICENSE
%{_includedir}/%{name}/
%{_datadir}/cmake/%{name}/
%{_datadir}/pkgconfig/%{name}.pc

%changelog
* Tue Mar 25 2025 Claude Code <noreply@anthropic.com> - 0.7.5-1
- Initial package for openEuler 24.03
- Based on skill_compile_third_party_libs.md compilation experience
