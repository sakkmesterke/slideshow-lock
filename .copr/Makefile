# Source-RPM builder for COPR ("Make srpm" source type). COPR runs, inside the checkout,
#   make -f .copr/Makefile srpm outdir=<dir> spec=<spec file>
# and takes the *.src.rpm it finds in <dir>.
#
# The source tarball is made from the checkout itself (git archive of HEAD), named as Source0 of the
# spec names it, so rpmbuild finds it in the source directory and nothing is downloaded. The file
# reads nothing from the network and needs no secret. It needs git and rpmbuild.
#
#   make -f .copr/Makefile tarball outdir=DIR   only the tarball: DIR/sources/<name>-<version>.tar.gz
#   make -f .copr/Makefile srpm outdir=DIR      the tarball, then DIR/<name>-<version>-<release>.src.rpm

root    := $(abspath $(dir $(lastword $(MAKEFILE_LIST)))/..)
spec    ?= packaging/fedora/slideshow-lock.spec
specfile = $(if $(filter /%,$(spec)),$(spec),$(root)/$(spec))
outdir  ?= .
out      = $(abspath $(outdir))
name     = $(shell sed -n 's/^Name:[[:space:]]*//p' '$(specfile)')
version  = $(shell sed -n 's/^Version:[[:space:]]*//p' '$(specfile)')

.PHONY: tarball srpm

tarball:
	@test -n '$(name)' && test -n '$(version)' || { echo 'no Name or Version in $(specfile)' >&2; exit 1; }
	mkdir -p '$(out)/sources'
	git -c safe.directory='*' -C '$(root)' archive --format=tar.gz \
	    --prefix='$(name)-$(version)/' -o '$(out)/sources/$(name)-$(version).tar.gz' HEAD

srpm: tarball
	rpmbuild -bs '$(specfile)' --define '_sourcedir $(out)/sources' --define '_srcrpmdir $(out)'
