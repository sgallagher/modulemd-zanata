# -*- coding: utf-8 -*-
# This file is part of modulemd-zanata
# Copyright (C) 2017-2018 Stephen Gallagher
#
# Fedora-License-Identifier: MIT
# SPDX-2.0-License-Identifier: MIT
# SPDX-3.0-License-Identifier: MIT
#
# This program is free software.
# For more information on the license, see COPYING.
# For more information on free software, see
# <https://www.gnu.org/philosophy/free-sw.en.html>.
#
# This module provides utility functions for interacting with Zanata
# translations of Fedora-style modules.

import sys
import gi
import requests

from babel.messages import Catalog
from datetime import datetime

gi.require_version('Modulemd', '1.0')
from gi.repository import Modulemd


def get_latest_modules_in_tag(session, tag, debug=False):
    """
    Get the most-recently built versions of each (module,stream) pair from
    a Koji tag
    :param session: A Koji session
    :param tag: A koji tag
    :return: A list of the most recent build of all modules in the tag.
    """

    # Koji sometimes disconnects for no apparent reason. Retry up to 5
    # times before failing.
    for attempt in range(5):
        try:
            tagged = session.listTagged(tag, latest=False)
        except requests.exceptions.ConnectionError:
            if debug:
                print("Connection lost while retrieving builds for tag %s, "
                      "retrying..." % tag,
                      file=sys.stderr)
        else:
            # Succeeded this time, so break out of the loop
            break

    # Find the latest, in module terms.  Pungi does this.
    # Collect all contexts that share the same NSV.
    NSVs = {}
    for entry in tagged:
        name, stream = entry['name'], entry['version']
        version = entry['release'].rsplit('.', 1)[0]

        NSVs[name] = NSVs.get(name, {})
        NSVs[name][stream] = NSVs[name].get(stream, {})
        NSVs[name][stream][version] = NSVs[name][stream].get(version, [])
        NSVs[name][stream][version].append(entry)

    latest = []
    for name in NSVs:
        for stream in NSVs[name]:
            version = sorted(list(NSVs[name][stream].keys()))[-1]
            latest.extend(NSVs[name][stream][version])

    return latest


def get_module_catalog_from_tags(session, tags, debug=False):
    """
    Construct a Babel translation source catalog from the contents of the
    provided tags.
    :param session: A Koji session
    :param tags: A set of Koji tags from which module metadata should be pulled
    :param debug: Whether to print debugging information to the console
    :return: A babel.messages.Catalog containing extracted translatable strings
    from any modules in the provided tags. Raises an exception if any of the
    retrieved modulemd is invalid.
    """

    catalog = Catalog(project="fedora-modularity-translations")

    tagged_builds = []
    for tag in tags:
        tagged_builds.extend(get_latest_modules_in_tag(session, tag, debug))

    # Make the list unique since some modules may have multiple tags
    unique_builds = {}
    for build in tagged_builds:
        unique_builds[build['id']] = build

    for build_id in unique_builds.keys():
        # Koji sometimes disconnects for no apparent reason. Retry up to 5
        # times before failing.
        for attempt in range(5):
            try:
                build = session.getBuild(build_id)
            except requests.exceptions.ConnectionError:
                if debug:
                    print("Connection lost while processing buildId %s, "
                          "retrying..." % build_id,
                          file=sys.stderr)
            else:
                # Succeeded this time, so break out of the loop
                break
        if debug:
            print("Processing %s:%s" % (build['package_name'], build['nvr']))

        modulemds = Modulemd.objects_from_string(
            build['extra']['typeinfo']['module']['modulemd_str'])

        # We should only get a single modulemd document from Koji
        if len(modulemds) != 1:
            raise ValueError("Koji build %s returned multiple modulemd YAML "
                             "documents." % build['nvr'])

        mmd = modulemds[0]

        # Process the summary
        msg = catalog.get(mmd.props.summary)
        if msg:
            locations = msg.locations
        else:
            locations = []
        locations.append(("%s;%s;summary" % (
                mmd.props.name,mmd.props.stream), 1))
        catalog.add(mmd.props.summary, locations=locations)

        # Process the description
        msg = catalog.get(mmd.props.description)
        if msg:
            locations = msg.locations
        else:
            locations = []
        locations.append(("%s;%s;description" % (
            mmd.props.name, mmd.props.stream), 2))
        catalog.add(mmd.props.description, locations=locations)

        # Get any profile descriptions
        for profile_name, profile in modulemds[0].peek_profiles().items():
            if profile.props.description:
                msg = catalog.get(profile.props.description)
                if msg:
                    locations = msg.locations
                else:
                    locations = []

                locations.append(("%s;%s;profile;%s" % (
                    mmd.props.name,
                    mmd.props.stream,
                    profile.props.name),
                                       3))
                catalog.add(profile.props.description, locations=locations)

    return catalog


def get_modulemd_translations_from_catalog_dict(catalog_dict):
    now = datetime.utcnow()
    modified = int("%04d%02d%02d%02d%02d%02d" % (
        now.date().year,
        now.date().month,
        now.date().day,
        now.time().hour,
        now.time().minute,
        now.time().second
    ))

    # Translation entries keyed by name, stream and locale
    entries = dict()

    mmd_translations = dict()
    for locale, catalog in catalog_dict.items():
        for msg in catalog:
            if not msg.locations or not msg.string:
                # Skip any message that doesn't actually contain a message
                continue

            for location, _ in msg.locations:
                split_location = location.split(';')
                if len(split_location) < 3 or len(split_location) > 5:
                    print("Invalid location clue in translation data: %s" % (
                        location), file=sys.stderr)

                module_name = split_location[0]
                module_stream = split_location[1]

                try:
                    entry = entries[(module_name, module_stream, locale)]
                except KeyError:
                    entry = Modulemd.TranslationEntry.new(locale)

                # Summary Translation
                if split_location[2] == "summary":
                    entry.set_summary(msg.string)

                # Description Translation
                elif split_location[2] == "description":
                    entry.set_description(msg.string)

                # Translation of profile descriptions
                elif split_location[2] == "profile":
                    entry.set_profile_description(split_location[3],
                                                  msg.string)

                entries[(module_name, module_stream, locale)] = entry

    for (module_name, module_stream, locale), entry in entries.items():
        # Validate that the translation entry has both summary and description
        # which are mandatory.
        if not entry.get_summary() or not entry.get_description():
            continue

        # Otherwise, add or update the translation for this module and stream
        try:
            mmdtranslation = mmd_translations[(module_name,
                                               module_stream)]
        except KeyError:
            mmdtranslation = Modulemd.Translation.new_full(
                module_name=module_name,
                module_stream=module_stream,
                mdversion=1,
                modified=modified
            )

        mmdtranslation.add_entry(entry)
        mmd_translations[(module_name, module_stream)] = mmdtranslation

    return mmd_translations.values()