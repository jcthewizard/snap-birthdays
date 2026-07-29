"""Real-world Facebook GraphQL birthday response, captured by fb2cal.

    Fixture data copied verbatim (apart from the trimming noted below) from
    fb2cal's ``tests/mocks/birthday_comet_root_mocks.py``:

        https://github.com/mobeigi/fb2cal
        Copyright (C) Mohammad Beigi and fb2cal contributors
        Licensed under the GNU General Public License v3.0

    ``birthdays`` derives from fb2cal and is likewise GPL-3.0, so this fixture is
    redistributed under the same terms.

    This program is free software: you can redistribute it and/or modify it under
    the terms of the GNU General Public License as published by the Free Software
    Foundation, either version 3 of the License, or (at your option) any later
    version.  See <http://www.gnu.org/licenses/>.

    Only change from upstream: ``data.upcomingAll.all_friends.edges`` had 596
    identical ``{"__typename": "AllFriendsEdge"}`` placeholder edges (none of them
    carrying a ``node``); 3 are kept so the file stays reviewable.

    The document carries BOTH shapes we have to parse:

    * ``data.{today,recent,upcoming,upcomingAll}.all_friends.edges[].node``
    * ``data.viewer.all_friends_by_birthday_month.edges[].node.friends.edges[].node``
"""

BIRTHDAY_COMET_ROOT_JANUARY_MOCK = {'data': {'today': {'all_friends': {'edges': []}},
          'recent': {'all_friends': {'edges': [{'__typename': 'AllFriendsEdge',
                                                'node': {'__typename': 'User',
                                                         'id': '100000000000001',
                                                         'birthdate': {'day': 1,
                                                                       'month': 1,
                                                                       'text': '1 January 2000',
                                                                       'year': 2000},
                                                         'birthday_campaign': None,
                                                         'has_viewer_posted_for_birthday': False,
                                                         'name': 'Test User',
                                                         '__isActor': 'User',
                                                         '__isEntity': 'User',
                                                         'profile_url': 'https://www.facebook.com/test.user',
                                                         'story_bucket': {'nodes': []},
                                                         'url': 'https://www.facebook.com/test.user',
                                                         'profile_picture': {'uri': 'https://scontent-syd2-1.xx.fbcdn.net/v/t1.30497-1/c29.0.100.100a/p100x100/84241059_189132118950875_4138507100605120512_n.jpg?_nc_cat=1&ccb=2&_nc_sid=7206a8&_nc_ohc=NcxDdcCWF5IAX9uLSTe&_nc_ht=scontent-syd2-1.xx&tp=27&oh=75cf4f4372f5eca63c50b94ca6d4949d&oe=5FD42D1E',
                                                                             'width': 100,
                                                                             'height': 100,
                                                                             'scale': 1.5},
                                                         'can_viewer_message': True,
                                                         'can_viewer_post': True,
                                                         'fundraisers_owned': {'nodes': []},
                                                         'gender': 'MALE',
                                                         '__isNode': 'User'}}]}},
          'upcoming': {'all_friends': {'edges': [{'__typename': 'AllFriendsEdge',
                                                  'node': {'__typename': 'User',
                                                           'id': '1353772287',
                                                           'birthdate': {'day': 2,
                                                                         'month': 2,
                                                                         'text': '2 February',
                                                                         'year': None},
                                                           'birthday_campaign': None,
                                                           'has_viewer_posted_for_birthday': False,
                                                           'name': 'Crazy Captain',
                                                           '__isActor': 'User',
                                                           '__isEntity': 'User',
                                                           'profile_url': 'https://www.facebook.com/crazy.captain',
                                                           'story_bucket': {'nodes': []},
                                                           'url': 'https://www.facebook.com/crazy.captain',
                                                           'profile_picture': {'uri': 'https://scontent-syd2-1.xx.fbcdn.net/v/t1.0-1/c0.17.100.100a/p100x100/480422_10201565103753467_1788826544_n.jpg?_nc_cat=103&ccb=2&_nc_sid=7206a8&_nc_ohc=oHFfGjW58VkAX8trdsg&_nc_ht=scontent-syd2-1.xx&tp=27&oh=b4921c5c9364d16a4617319b922d983a&oe=5FD52631',
                                                                               'width': 100,
                                                                               'height': 100,
                                                                               'scale': 1.5},
                                                           'can_viewer_message': True,
                                                           'can_viewer_post': True,
                                                           'fundraisers_owned': {'nodes': []},
                                                           'gender': 'MALE',
                                                           '__isNode': 'User'}}]}},
          'upcomingAll': {'all_friends': {'edges': [{'__typename': 'AllFriendsEdge'},
                                                    {'__typename': 'AllFriendsEdge'},
                                                    {'__typename': 'AllFriendsEdge'}]}},
          'viewer': {'actor': {'__typename': 'User', 'id': '1000000017'},
                     'all_friends': {'edges': []},
                     'all_friends_by_birthday_month': {'page_info': {'has_next_page': True,
                                                                     'end_cursor': '2'},
                                                       'edges': [{'node': {'month_name_in_iso8601': 'November',
                                                                           'friends_by_birthday_month_context_sentence': {'text': 'Pirate '
                                                                                                                                  'Pete, '
                                                                                                                                  'Lorem '
                                                                                                                                  'Ipsum '
                                                                                                                                  'and '
                                                                                                                                  '30 '
                                                                                                                                  'others',
                                                                                                                          'ranges': [{'length': 18,
                                                                                                                                      'offset': 0,
                                                                                                                                      'entity': {'__typename': 'User',
                                                                                                                                                 'url': 'https://www.facebook.com/pirate.pete',
                                                                                                                                                 '__isNode': 'User',
                                                                                                                                                 'id': '600009847'}},
                                                                                                                                     {'length': 15,
                                                                                                                                      'offset': 20,
                                                                                                                                      'entity': {'__typename': 'User',
                                                                                                                                                 'url': 'https://www.facebook.com/lorem.ipsum',
                                                                                                                                                 '__isNode': 'User',
                                                                                                                                                 'id': '1000021917'}}]},
                                                                           'friends': {'edges': [{'node': {'__typename': 'User',
                                                                                                           'id': '600009847',
                                                                                                           '__isActor': 'User',
                                                                                                           '__isEntity': 'User',
                                                                                                           'profile_url': 'https://www.facebook.com/pirate.pete',
                                                                                                           'url': 'https://www.facebook.com/pirate.pete',
                                                                                                           'name': 'Pirate '
                                                                                                                   'Pete',
                                                                                                           'profile_picture': {'uri': 'https://scontent-syd2-1.xx.fbcdn.net/v/t1.0-1/cp0/p60x60/122897864_10161077510019848_299841799681806933_o.jpg?_nc_cat=107&ccb=2&_nc_sid=7206a8&_nc_ohc=yzAYhtdvoMYAX9Zxo1e&_nc_ht=scontent-syd2-1.xx&tp=27&oh=dc48247e31223151bc5d55781a572e2f&oe=5FD254D0',
                                                                                                                               'width': 60,
                                                                                                                               'height': 60,
                                                                                                                               'scale': 1},
                                                                                                           'birthdate': {'day': 1,
                                                                                                                         'month': 11,
                                                                                                                         'year': 1982},
                                                                                                           '__module_operation_BirthdayCometMonthlyBirthdaysCard_allFriendsByBirthdayMonthEdge': {'__dr': 'BirthdayCometProfilePictureOnUser_user$normalization.graphql'},
                                                                                                           '__module_component_BirthdayCometMonthlyBirthdaysCard_allFriendsByBirthdayMonthEdge': {'__dr': 'BirthdayCometProfilePictureOnUser.react'}}}]},
                                                                           '__typename': 'FriendsByBirthdayMonth'},
                                                                  'cursor': '0'},
                                                                 {'node': {'month_name_in_iso8601': 'December',
                                                                           'friends_by_birthday_month_context_sentence': {'text': 'Santa '
                                                                                                                                  'Claus, '
                                                                                                                                  'Lorem '
                                                                                                                                  'Ipsum '
                                                                                                                                  'and '
                                                                                                                                  '30 '
                                                                                                                                  'others',
                                                                                                                          'ranges': [{'length': 18,
                                                                                                                                      'offset': 0,
                                                                                                                                      'entity': {'__typename': 'User',
                                                                                                                                                 'url': 'https://www.facebook.com/pirate.pete',
                                                                                                                                                 '__isNode': 'User',
                                                                                                                                                 'id': '600009847'}},
                                                                                                                                     {'length': 15,
                                                                                                                                      'offset': 20,
                                                                                                                                      'entity': {'__typename': 'User',
                                                                                                                                                 'url': 'https://www.facebook.com/lorem.ipsum',
                                                                                                                                                 '__isNode': 'User',
                                                                                                                                                 'id': '1000021917'}}]},
                                                                           'friends': {'edges': [{'node': {'__typename': 'User',
                                                                                                           'id': '1000023',
                                                                                                           '__isActor': 'User',
                                                                                                           '__isEntity': 'User',
                                                                                                           'profile_url': 'https://www.facebook.com/santa',
                                                                                                           'url': 'https://www.facebook.com/santa',
                                                                                                           'name': 'Santa '
                                                                                                                   'Claus',
                                                                                                           'profile_picture': {'uri': 'https://scontent-syd2-1.xx.fbcdn.net/v/t1.0-1/cp0/p60x60/53497864_10161077510019848_299841799451806933_o.jpg?_nc_cat=107&ccb=2&_nc_sid=7206a8&_nc_ohc=yzAYhtdvoMYAX9Zxo1e&_nc_ht=scontent-syd2-1.xx&tp=27&oh=dc48247e31223151bc5d55781a572e2f&oe=5FD254D0',
                                                                                                                               'width': 60,
                                                                                                                               'height': 60,
                                                                                                                               'scale': 1},
                                                                                                           'birthdate': {'day': 25,
                                                                                                                         'month': 12,
                                                                                                                         'year': None},
                                                                                                           '__module_operation_BirthdayCometMonthlyBirthdaysCard_allFriendsByBirthdayMonthEdge': {'__dr': 'BirthdayCometProfilePictureOnUser_user$normalization.graphql'},
                                                                                                           '__module_component_BirthdayCometMonthlyBirthdaysCard_allFriendsByBirthdayMonthEdge': {'__dr': 'BirthdayCometProfilePictureOnUser.react'}}}]},
                                                                           '__typename': 'FriendsByBirthdayMonth'},
                                                                  'cursor': '0'},
                                                                 {'node': {'month_name_in_iso8601': 'January',
                                                                           'friends_by_birthday_month_context_sentence': {'text': 'Albus '
                                                                                                                                  'Dumbledore, '
                                                                                                                                  'Lorem '
                                                                                                                                  'Ipsum '
                                                                                                                                  'and '
                                                                                                                                  '30 '
                                                                                                                                  'others',
                                                                                                                          'ranges': [{'length': 18,
                                                                                                                                      'offset': 0,
                                                                                                                                      'entity': {'__typename': 'User',
                                                                                                                                                 'url': 'https://www.facebook.com/prof.albus',
                                                                                                                                                 '__isNode': 'User',
                                                                                                                                                 'id': '198041065'}},
                                                                                                                                     {'length': 15,
                                                                                                                                      'offset': 20,
                                                                                                                                      'entity': {'__typename': 'User',
                                                                                                                                                 'url': 'https://www.facebook.com/lorem.ipsum',
                                                                                                                                                 '__isNode': 'User',
                                                                                                                                                 'id': '1000021917'}}]},
                                                                           'friends': {'edges': [{'node': {'__typename': 'User',
                                                                                                           'id': '198041065',
                                                                                                           '__isActor': 'User',
                                                                                                           '__isEntity': 'User',
                                                                                                           'profile_url': 'https://www.facebook.com/prof.albus',
                                                                                                           'url': 'https://www.facebook.com/prof.albus',
                                                                                                           'name': 'Albus '
                                                                                                                   'Dumbledore',
                                                                                                           'profile_picture': {'uri': 'https://scontent-syd2-1.xx.fbcdn.net/v/t1.0-1/cp0/p60x60/34f34864_10161077510019848_299841799681806933_o.jpg?_nc_cat=107&ccb=2&_nc_sid=7406a8&_nc_ohc=yzAYhtdvoMYAX9Zxo1e&_nc_ht=scontent-syd2-1.xx&tp=27&oh=dc48247e31223151bc5d55781a572e2f&oe=5FD254D0',
                                                                                                                               'width': 60,
                                                                                                                               'height': 60,
                                                                                                                               'scale': 1},
                                                                                                           'birthdate': {'day': 17,
                                                                                                                         'month': 1,
                                                                                                                         'year': 1881},
                                                                                                           '__module_operation_BirthdayCometMonthlyBirthdaysCard_allFriendsByBirthdayMonthEdge': {'__dr': 'BirthdayCometProfilePictureOnUser_user$normalization.graphql'},
                                                                                                           '__module_component_BirthdayCometMonthlyBirthdaysCard_allFriendsByBirthdayMonthEdge': {'__dr': 'BirthdayCometProfilePictureOnUser.react'}}}]},
                                                                           '__typename': 'FriendsByBirthdayMonth'},
                                                                  'cursor': '0'}]}}}}
