Name:           ROS_PACKAGE_NAME
Version:        ROS_PACKAGE_VERSION
Release:        ROS_PACKAGE_RELEASE%{?dist}
Summary:        C++ wrappers for SIMD intrinsic
License:        BSD-3-Clause
URL:            https://xsimd.readthedocs.io/

Source0:        %{name}-%{version}.tar.gz

BuildRequires:  cmake
BuildRequires:  gcc-c++

# there is no actual arched content - this is a header only library
%global debug_package %{nil}

%global _description \
SIMD (Single Instruction, Multiple Data) is a feature of microprocessors that \
has been available for many years. SIMD instructions perform a single operation \
on a batch of values at once, and thus provide a way to significantly \
accelerate code execution. However, these instructions differ between \
microprocessor vendors and compilers. \
 \
xsimd provides a unified means for using these features for library authors. \
Namely, it enables manipulation of batches of numbers with the same arithmetic \
operators as for single values. It also provides accelerated implementation \
of common mathematical functions operating on batches.

%description %_description

%package devel
Summary:        %{summary}
Provides:       %{name} = %{version}-%{release}
Provides:       %{name}-static = %{version}-%{release}
BuildArch:      noarch

%description devel %_description

%prep
%autosetup -n %{name}-%{version}

%build
%cmake -DBUILD_TESTS=OFF
%make_build

%install
%make_install

%files devel
%doc README.md
%license LICENSE
%{_includedir}/%{name}/
%{_datadir}/cmake/%{name}/
%{_datadir}/pkgconfig/%{name}.pc

%changelog
* Tue Mar 25 2025 Claude Code <noreply@anthropic.com> - 14.1.0-1
- Upgrade to 14.1.0
- Remove 0001-fix-copy-pasted-headers.patch (fixed upstream)

* Thu May 29 2025 Dongxing Wang <dongxing.wang_a@thundersoft.com> - 13.2.0-1
- Init package
