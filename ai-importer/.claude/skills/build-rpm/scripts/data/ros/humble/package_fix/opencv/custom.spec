Name:           ROS_PACKAGE_NAME
Version:        ROS_PACKAGE_VERSION
Release:        ROS_PACKAGE_RELEASE%{?dist}
Summary:	OpenCV means Intel® Open Source Computer Vision Library.
License:	Apache-2.0
URL:		https://github.com/opencv/opencv
Source0:        %{name}-%{version}.tar.gz
Patch1:     Fix-OpenCV-build-with-OpenEXR-before-2.2.0.patch
Patch2:     Fix_compilation_of_copy_assignment_operators_with_GCC.patch
Patch3:     Repair_clang_abi.patch
Patch4:     CVE-2022-0561_and_CVE-2022-0562.patch
Patch5:     CVE-2022-0908.patch
Patch6:     Merge-pull-request-21114-from-dwardor-patch-1.patch
Patch7:     calib3d-use-OCV_LAPACK_FUNC.patch

BuildRequires:  qt5-qtbase-devel
BuildRequires:	gcc-c++ gcc autoconf pkgconfig protobuf-compiler protobuf
BuildRequires:  cmake
BuildRequires:  python3-numpy python3-devel
BuildRequires:  tesseract-devel
BuildRequires:  mesa-libGLU-devel
BuildRequires:  java-1.8.0-openjdk

%description
OpenCV means Intel® Open Source Computer Vision Library. It is a collection of
C functions and a few C++ classes that implement some popular Image Processing
and Computer Vision algorithms.

%global debug_package %{nil}

%prep
%autosetup -p1 -n %{name}-%{version}

%build
mkdir -p cmake/build
cd cmake/build
cmake ../../ -DCMAKE_BUILD_TYPE=Release\
             -DWITH_PROTOBUF=ON\
             -DWITH_WEBP=ON\
             -DWITH_IPP=OFF\
             -DWITH_ADE=OFF\
             -DBUILD_ZLIB=ON\
             -DBUILD_JPEG=ON\
             -DBUILD_PNG=ON\
             -DBUILD_OPENEXR=ON\
             -DBUILD_TESTS=OFF\
             -DBUILD_PERF_TESTS=OFF\
             -DBUILD_opencv_apps=OFF\
             -DWITH_CUDA=OFF\
             -DBUILD_JAVA=ON\
             -DBUILD_opencv_dnn=ON\
             -DBUILD_opencv_dnn_modern=ON\
             -DBUILD_opencv_face=ON\
             -DBUILD_opencv_python3=ON\
             -DBUILD_opencv_python2=OFF\
             -DBUILD_opencv_java=ON\
             -DWITH_GTK=OFF\
             -DWITH_OPENGL=ON\
             -DWITH_FFMPEG=OFF\
             -DWITH_TIFF=ON\
             -DWITH_QT=5\
             -DBUILD_TIFF=OFF\
             -DWITH_JASPER=OFF\
             -DBUILD_JASPER=OFF\
             -DBUILD_SHARED_LIBS=ON\
             -DBUILD_EXAMPLES=OFF\
             -DPYTHON3_EXECUTABLE=$(which python3)\
             -DPYTHON_EXECUTABLE=$(which python3)\
             -DPYTHON_DEFAULT_EXECUTABLE=$(python3 -c "import sys; print(sys.executable)")\
             -DPYTHON3_NUMPY_INCLUDE_DIRS=$(python3 -c "import numpy; print (numpy.get_include())")\
             -DPYTHON3_INCLUDE_DIR=$(python3 -c "from distutils.sysconfig import get_python_inc; print(get_python_inc())")\
             -DPYTHON_INCLUDE_DIR=$(python3 -c "from distutils.sysconfig import get_python_inc; print(get_python_inc())")\
             -DPYTHON3_LIBRARIES=$(python3 -c "import distutils.sysconfig as sysconfig; print(sysconfig.get_config_var('LIBDIR')+ '/libpython3.so')")\
             -DPYTHON3_LIBRARY=$(python3 -c "import distutils.sysconfig as sysconfig; print(sysconfig.get_config_var('LIBDIR')+ '/libpython3.so')")\
             -DPYTHON_LIBRARIES=$(python3 -c "import distutils.sysconfig as sysconfig; print(sysconfig.get_config_var('LIBDIR')+ '/libpython3.so')")\
             -DPYTHON_LIBRARY=$(python3 -c "import distutils.sysconfig as sysconfig; print(sysconfig.get_config_var('LIBDIR')+ '/libpython3.so')")\
             -DCMAKE_INSTALL_PREFIX=/usr \
             -DOPENCV_CONFIG_INSTALL_PATH=%{_lib}/cmake/OpenCV \
             -DOPENCV_GENERATE_PKGCONFIG=ON
make -j1 V=1


%install
cd cmake/build
make install DESTDIR=%{buildroot}

%files
%defattr(-,root,root)
%exclude /usr/bin/setup_vars_opencv4.sh
%{_bindir}/*
%{_libdir}/*
%{_includedir}/*
%exclude /usr/share/*
%{python3_sitelib}/cv2/*

%changelog
* Sun Mar 30 2026 openEuler ROS SIG - 4.5.2-10
- Reduce parallelism to make -j1 for EUR 2GB RAM builds
- Disable tests and examples to save memory
- Remove opencv_extra test data sources

* Wed Nov 22 2023 konglidong <konglidong@uniontech.com> - 4.5.2-9
- backport upstraem patch to fix build failed

* Sat May 06 2023 misaka00251 <liuxin@iscas.ac.cn> - 4.5.2-8
- Fix tests failed
- Add option to build DNN

* Thu Nov 05 2022 shenwei <shenwei41@huawei.com> - 4.5.2-7
- fix three cve bug of the opencv

* Thu Jan 28 2022 douyan <douyan@kylinos.cn> - 4.5.2-6
- add pkgconfig file

* Thu Jan 27 2022 douyan <douyan@kylinos.cn> - 4.5.2-5
- use %{python3_sitelib} instead of /usr/lib/python3.8/site-packages

* Wed Nov 17 2021 shenwei <shenwei41@huawei.com> - 4.5.2-4
- repair Clang ABI

* Sat Nov 13 2021 shenwei <shenwei41@huawei.com> - 4.5.2-3
- fix compilation of copy ctors/assignment operators with GCC 4.x

* Wed Nov 10 2021 yanhailiang <yanhailiang@huawei.com> - 4.5.2-2
- bugFix OpenCV build with OpenEXR before 2.2.0

* Thu Sep 30 2021 shenwei <shenwei41@huawei.com> - 4.5.2-1
- package init
