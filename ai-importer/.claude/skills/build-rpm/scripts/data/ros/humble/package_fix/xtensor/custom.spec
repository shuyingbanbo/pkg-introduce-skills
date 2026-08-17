Name:           ROS_PACKAGE_NAME
Version:        ROS_PACKAGE_VERSION
Release:        ROS_PACKAGE_RELEASE%{?dist}
Summary:        Multi-dimensional array library for C++
License:        BSD-3-Clause
URL:            https://github.com/xtensor-stack/xtensor

Source0:        %{name}-%{version}.tar.gz

BuildRequires:  cmake
BuildRequires:  gcc-c++
BuildRequires:  xtl-devel >= 0.7.0

# Header-only library - no debug package needed
%global debug_package %{nil}

%description
xtensor is a C++ library meant for numerical analysis with
multi-dimensional array expressions.

xtensor provides:
- An extensible expression system enabling lazy broadcasting.
- An API following the idioms of the C++ standard library.
- Tools to manipulate array expressions and build upon xtensor.

%package devel
Summary:        %{summary}
Provides:       %{name} = %{version}-%{release}
Provides:       %{name}-static = %{version}-%{release}
BuildArch:      noarch
Requires:       xtl-devel >= 0.7.0

%description devel
Development files for xtensor library. This is a header-only library.

%prep
%autosetup -n %{name}-%{version}

%build
%cmake -DBUILD_TESTS=OFF \
       -DBUILD_BENCHMARK=OFF \
       -DXTENSOR_USE_XSIMD=OFF \
       -DXTENSOR_USE_TBB=OFF \
       -DXTENSOR_USE_OPENMP=OFF

%install
%make_install

%files devel
%doc README.md
%license LICENSE
%{_includedir}/%{name}/
%{_includedir}/%{name}.hpp
%{_libdir}/cmake/%{name}/
%{_libdir}/pkgconfig/%{name}.pc

%changelog
* Tue Mar 25 2025 Claude Code <noreply@anthropic.com> - 0.24.0-1
- Initial package for openEuler 24.03
- Based on skill_compile_third_party_libs.md compilation experience
- Requires xtl >= 0.7.0
