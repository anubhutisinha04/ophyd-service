.. Packaging Scientific Python documentation master file, created by
   sphinx-quickstart on Thu Jun 28 12:35:56 2018.
   You can adapt this file completely to your liking, but it should at least
   contain the root `toctree` directive.

===================================
queueserver-service Documentation
===================================

The queueserver-service backend of ophyd-service: the bluesky Run Engine
queue manager together with its HTTP/WebSocket API server, maintained as a
single package (based on bluesky-queueserver and bluesky-httpserver).

.. toctree::
   :maxdepth: 1

   installation
   tutorials
   release_history
   contributing

.. toctree::
   :maxdepth: 1
   :caption: User's Guide

   introduction_for_users
   using_queue_server
   features_and_config
   startup_code
   item_validation
   plan_annotation
   cli_tools
   manager_config
   qserver_quick_ref

.. toctree::
   :maxdepth: 1
   :caption: Application Developer's Guide

   interacting_with_qs
   re_manager_api

.. toctree::
   :maxdepth: 2
   :caption: HTTP Server

   http/index

.. toctree::
   :maxdepth: 1
   :caption: Related Projects

   Bluesky Queue Server API <https://blueskyproject.io/bluesky-queueserver-api>
   Bluesky Widgets <https://blueskyproject.io/bluesky-widgets>
   Bluesky <https://blueskyproject.io/bluesky>
   Ophyd <https://blueskyproject.io/ophyd>
   Data Broker <https://blueskyproject.io/databroker>
