# -*- coding: utf-8 -*-
from django.test import TestCase
from models import Post, Comment
from factories import PostFactory, UserFactory, GroupFactory
from vkontakte_users.models import User
from datetime import datetime
import simplejson as json

USER_ID = 18658732
POST_ID = '18658732_2019'
GROUP_ID = 16297716
GROUP_SCREEN_NAME = 'cocacola'
GROUP_POST_ID = '-16297716_126261'
OPEN_WALL_GROUP_ID = 19391365
OPEN_WALL_GROUP_SCREEN_NAME = 'nokia'


class VkontakteWallTest(TestCase):

    def test_fetch_user_wall(self):

        owner = UserFactory.create(remote_id=USER_ID)

        self.assertEqual(Post.objects.count(), 0)

        posts = owner.fetch_posts()

        self.assertTrue(len(posts) > 0)
        self.assertEqual(Post.objects.count(), len(posts))
        self.assertEqual(posts[0].wall_owner, owner)

    def test_fetch_group_wall(self):

        group = GroupFactory.create(remote_id=GROUP_ID, screen_name=GROUP_SCREEN_NAME)

        self.assertEqual(Post.objects.count(), 0)

        posts = group.fetch_posts(count=10)

        self.assertTrue(len(posts), 10)
        self.assertEqual(Post.objects.count(), 10)
        self.assertEqual(posts[0].wall_owner, group)
        self.assertTrue(isinstance(posts[0].date, datetime))
        self.assertTrue(posts[0].likes + posts[1].likes > 0)
        self.assertTrue(posts[0].comments + posts[1].comments > 0)
        self.assertTrue(len(posts[0].text) > 0)

    def test_fetch_group_open_wall(self):

        group = GroupFactory.create(remote_id=OPEN_WALL_GROUP_ID, screen_name=OPEN_WALL_GROUP_SCREEN_NAME)

        self.assertEqual(Post.objects.count(), 0)
        self.assertEqual(User.objects.count(), 0)

        count = 10
        posts = group.fetch_posts(own=0, count=count)

        self.assertEqual(len(posts), count)
        self.assertEqual(Post.objects.count(), count)
        self.assertTrue(User.objects.count() > 0)
        self.assertTrue(Post.objects.exclude(author_id=None).count() > 0)

    def test_fetch_user_post_comments(self):

        owner = UserFactory.create(remote_id=USER_ID)
        post = PostFactory.create(remote_id=POST_ID, wall_owner=owner, author=owner)
        self.assertEqual(Comment.objects.count(), 0)

        comments = post.fetch_comments()

        self.assertTrue(len(comments) > 0)
        self.assertEqual(Comment.objects.count(), len(comments))
        self.assertEqual(comments[0].post, post)

    def test_fetch_group_post_comments(self):

        group = GroupFactory.create(remote_id=GROUP_ID, screen_name=GROUP_SCREEN_NAME)
        post = PostFactory.create(remote_id=GROUP_POST_ID, wall_owner=group)
        self.assertEqual(Comment.objects.count(), 0)

        comments = post.fetch_comments()

        self.assertTrue(len(comments) > 0)
        self.assertEqual(Comment.objects.count(), len(comments))
        self.assertEqual(comments[0].post, post)
        self.assertEqual(post.comments, len(comments))

#    def test_fetch_group_post_comments_after(self):
#
#        group = GroupFactory.create(remote_id=GROUP_ID, screen_name=GROUP_SCREEN_NAME)
#        post = PostFactory.create(remote_id=GROUP_POST_ID, wall_owner=group)
#        self.assertEqual(Comment.objects.count(), 0)
#
#        comments = post.fetch_comments(after=datetime(2012,7,23,0,0))
#
#        self.assertTrue(len(comments) > 10)
#        self.assertEqual(Comment.objects.count(), len(comments))
#        self.assertEqual(comments[0].post, post)
#        self.assertEqual(post.comments, len(comments))

    def test_update_post_reposts(self):

        post = PostFactory.create(remote_id=GROUP_POST_ID)

        self.assertEqual(post.reposts, 0)
        self.assertEqual(post.repost_users.count(), 0)
        post.update_reposts()
        self.assertNotEqual(post.reposts, 0)
        self.assertNotEqual(post.repost_users.count(), 0)

    def test_update_post_likes(self):

        post = PostFactory.create(remote_id=GROUP_POST_ID)

        self.assertEqual(post.likes, 0)
        self.assertEqual(post.like_users.count(), 0)
        post.update_likes()
        self.assertNotEqual(post.likes, 0)
        self.assertNotEqual(post.like_users.count(), 0)
        self.assertTrue(post.like_users.count() > 24)

    def test_parse_post(self):

        response = '''{"comments": {"can_post": 0, "count": 4},
                 "date": 1298365200,
                 "from_id": 55555,
                 "geo": {"coordinates": "55.6745689498 37.8724562529",
                  "place": {"city": "Moskovskaya oblast",
                   "country": "Russian Federation",
                   "title": "Shosseynaya ulitsa, Moskovskaya oblast"},
                  "type": "point"},
                 "id": 465,
                 "likes": {"can_like": 1, "can_publish": 1, "count": 10, "user_likes": 0},
                 "online": 1,
                 "post_source": {"type": "api"},
                 "reply_count": 0,
                 "reposts": {"count": 3, "user_reposted": 0},
                 "text": "qwerty",
                 "to_id": 2462759}
            '''
        instance = Post()
        owner = UserFactory.create(remote_id=2462759)
        author = UserFactory.create(remote_id=55555)
        instance.parse(json.loads(response))
        instance.save()

        self.assertEqual(instance.remote_id, '2462759_465')
        self.assertEqual(instance.wall_owner, owner)
        self.assertEqual(instance.author, author)
        self.assertEqual(instance.reply_count, 0)
        self.assertEqual(instance.likes, 10)
        self.assertEqual(instance.reposts, 3)
        self.assertEqual(instance.comments, 4)
        self.assertEqual(instance.text, 'qwerty')
        self.assertEqual(instance.date, datetime(2011,2,22,12,0,0))

    def test_parse_comments(self):

        response = '''{"response":[6,
            {"cid":2505,"uid":16271479,"date":1298365200,"text":"Добрый день , кароче такая идея когда опросы создаешь вместо статуса - можно выбрать аудитории опрашиваемых, например только женский или мужской пол могут участвовать (то бишь голосовать в опросе)."},
            {"cid":2507,"uid":16271479,"date":1286105582,"text":"Это уже не практично, имхо.<br>Для этого делайте группу и там опрос, а в группу принимайте тех, кого нужно.","reply_to_uid":16271479,"reply_to_cid":2505},
            {"cid":2547,"uid":2943,"date":1286218080,"text":"Он будет только для групп благотворительных организаций."}]}
            '''
        post = PostFactory(remote_id='1_0')
        instance = Comment(post=post)
        author = UserFactory.create(remote_id=16271479)
        instance.parse(json.loads(response)['response'][1])
        instance.save()

        self.assertEqual(instance.remote_id, '1_2505')
        self.assertEqual(instance.text, u'Добрый день , кароче такая идея когда опросы создаешь вместо статуса - можно выбрать аудитории опрашиваемых, например только женский или мужской пол могут участвовать (то бишь голосовать в опросе).')
        self.assertEqual(instance.date, datetime(2011,2,22,12,0,0))
        self.assertEqual(instance.author, author)

        instance.parse(json.loads(response)['response'][2])
        instance.save()

        self.assertEqual(instance.remote_id, '1_2507')
        self.assertEqual(instance.reply_for.remote_id, 16271479)
#        self.assertEqual(instance.reply_to.remote_id, '...2505')