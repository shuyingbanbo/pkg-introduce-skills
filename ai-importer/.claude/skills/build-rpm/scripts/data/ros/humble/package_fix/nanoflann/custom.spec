Name:           ROS_PACKAGE_NAME
Version:        ROS_PACKAGE_VERSION
Release:        ROS_PACKAGE_RELEASE%{?dist}
Summary:        C++ header-only library for KD-Trees
License:        BSD-3-Clause
URL:            https://github.com/jlblancoc/nanoflann

Source0:        %{name}-%{version}.tar.gz

BuildRequires:  cmake
BuildRequires:  gcc-c++

# Header-only library - no debug package needed
%global debug_package %{nil}

%description
nanoflann is a C++11 header-only library for building KD-Trees of
datasets with different topologies: R2, R3 (point clouds), SO(2)
and SO(3) (2D and 3D rotation groups).

Key features:
- Fast query times and low memory usage
- Support for different distance metrics (L1, L2)
- No dependencies beyond standard library

%package devel
Summary:        %{summary}
Provides:       %{name} = %{version}-%{release}
Provides:       %{name}-static = %{version}-%{release}
BuildArch:      noarch

%description devel
Development files for nanoflann library. This is a header-only library.

%prep
%autosetup -n %{name}-%{version}

%build
%cmake -DNANOFLANN_BUILD_EXAMPLES=OFF \
       -DNANOFLANN_BUILD_TESTS=OFF

%install
%make_install

%files devel
%doc README.md CHANGELOG.md
%license COPYING
%{_includedir}/%{name}.hpp
%{_datadir}/cmake/%{name}/
%{_libdir}/pkgconfig/%{name}.pc

%changelog
* Tue Mar 25 2025 Claude Code <noreply@anthropic.com> - 1.9.0-1
- Initial package for openEuler 24.03
- Based on skill_compile_third_party_libs.md compilation experience
