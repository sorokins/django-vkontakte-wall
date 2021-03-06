# -*- coding: utf-8 -*-
from django.db import models, transaction
from django.dispatch import Signal
from django.contrib.contenttypes.models import ContentType
from django.contrib.contenttypes import generic
from django.core.exceptions import ObjectDoesNotExist
from django.conf import settings
from vkontakte_api.utils import api_call
from vkontakte_api import fields
from vkontakte_api.models import VkontakteTimelineManager, VkontakteModel, VkontakteCRUDModel, VkontakteCRUDManager, VkontakteContentError, MASTER_DATABASE
from vkontakte_api.decorators import fetch_all
from vkontakte_users.models import User, ParseUsersMixin
from vkontakte_groups.models import Group, ParseGroupsMixin
from m2m_history.fields import ManyToManyHistoryField
from parser import VkontakteWallParser, VkontakteParseError
from datetime import datetime
import logging
import re

log = logging.getLogger('vkontakte_wall')

parsed = Signal(providing_args=['sender', 'instance', 'container'])


class PostRemoteManager(VkontakteTimelineManager, ParseUsersMixin, ParseGroupsMixin):

    response_instances_fieldname = 'wall'

    def fetch(self, ids=None, *args, **kwargs):
        '''
        Retrieve and save object to local DB
        '''
        if ids:
            kwargs['posts'] = ','.join(ids)
            kwargs['method'] = 'getById'

        return super(PostRemoteManager, self).fetch(*args, **kwargs)

    def parse_response_dict(self, resource, extra_fields=None):
        if self.response_instances_fieldname in resource:
            # if extended = 1 in request
            self.parse_response_users(resource)
            self.parse_response_groups(resource)
            return super(PostRemoteManager, self).parse_response_list(resource[self.response_instances_fieldname], extra_fields)
        else:
            return super(PostRemoteManager, self).parse_response_dict(resource, extra_fields)

    @transaction.commit_on_success
    @fetch_all(default_count=100)
    def fetch_wall(self, owner, offset=0, count=100, filter='all', extended=False, before=None, after=None, **kwargs):
        if filter not in ['owner', 'others', 'all']:
            raise ValueError("Attribute 'fiter' has illegal value '%s'" % filter)
        if count > 100:
            raise ValueError("Attribute 'count' can not be more than 100")
        if before and not after:
            raise ValueError("Attribute `before` should be specified with attribute `after`")
        if before and before < after:
            raise ValueError("Attribute `before` should be later, than attribute `after`")

        kwargs['owner_id'] = owner.remote_id
        kwargs['filter'] = filter
        kwargs['extended'] = int(extended)
        kwargs['offset'] = int(offset)
        kwargs.update({'count': count})
        if isinstance(owner, Group):
            kwargs['owner_id'] *= -1
        # special parameters
        kwargs['after'] = after
        kwargs['before'] = before

        log.debug('Fetching posts of owner "%s", offset %d' % (owner, offset))

        return self.fetch(**kwargs)

    @transaction.commit_on_success
    def fetch_group_wall_parser(self, group, offset=0, count=None, own=False, after=None):
        '''
        Old method via parser
        TODO: `before` parameter not implemented
        '''
        post_data = {
            'al': 1,
            'offset': offset,
            'own': int(own),  # posts by only group or any users
            'part': 1,  # without header, footer
        }

        log.debug('Fetching post of group "%s", offset %d' % (group, offset))

        parser = VkontakteWallParser().request('/wall-%s' % group.remote_id, data=post_data)

        items = parser.content_bs.findAll('div', {'class': re.compile('^post'), 'id': re.compile('^post-%d' % group.remote_id)})

        current_count = offset + len(items)
        need_cut = count and count < current_count
        if need_cut:
            items = items[:count - offset]

        for item in items:

            try:
                post = parser.parse_post(item, group)
            except VkontakteParseError, e:
                log.error(e)
                continue

            if after and post.date < after:
                need_cut = True
                break

            post.raw_html = unicode(item)
            post.save()
            parsed.send(sender=Post, instance=post, container=item)

        if len(items) == 20 and not need_cut:
            return self.fetch_group_wall(group, offset=current_count, count=count, own=own, after=after)
        elif after:
            return group.wall_posts.filter(date__gte=after)
        else:
            return group.wall_posts.all()


class CommentRemoteManager(VkontakteTimelineManager):

    @transaction.commit_on_success
    @fetch_all(default_count=100)
    def fetch_post(self, post, offset=0, count=100, sort='asc', need_likes=True, preview_length=0, before=None, after=None, **kwargs):
        if count > 100:
            raise ValueError("Attribute 'count' can not be more than 100")
        if sort not in ['asc', 'desc']:
            raise ValueError("Attribute 'sort' should be equal to 'asc' or 'desc'")
        if sort == 'asc' and (after or before):
            raise ValueError("Attribute `sort` should be equal to 'desc' with defined `after` attribute")
        if before and not after:
            raise ValueError("Attribute `before` should be specified with attribute `after`")
        if before and before < after:
            raise ValueError("Attribute `before` should be later, than attribute `after`")

        # owner_id
        # идентификатор пользователя, на чьей стене находится запись, к которой необходимо получить комментарии. Если параметр не задан, то он считается равным идентификатору текущего пользователя.
        kwargs['owner_id'] = post.wall_owner.remote_id
        if isinstance(post.wall_owner, Group):
            kwargs['owner_id'] *= -1
        # post_id
        # идентификатор записи на стене пользователя.
        kwargs['post_id'] = post.remote_id.split('_')[1]
        # sort
        # порядок сортировки комментариев:
        # asc - хронологический
        # desc - антихронологический
        kwargs['sort'] = sort
        # offset
        # смещение, необходимое для выборки определенного подмножества комментариев.
        kwargs['offset'] = int(offset)
        # need_likes
        # 1 - будет возвращено дополнительное поле likes. По умолчанию поле likes не возвращается.
        kwargs['need_likes'] = int(need_likes)
        # count
        # количество комментариев, которое необходимо получить (но не более 100).
        kwargs.update({'count': count})
        # preview_length
        # Количество символов, по которому нужно обрезать комментарии. Укажите 0, если Вы не хотите обрезать комментарии. (по умолчанию 90). Обратите внимание, что комментарии обрезаются по словам.
        kwargs['preview_length'] = int(preview_length)
        # v
        # Данный метод может возвращать разные результаты в зависимости от используемой версии. Передавайте v=4.4 для того, чтобы получать аттачи в комментариях в виде объектов, а не ссылок.

        kwargs['extra_fields'] = {'post_id': post.id}
        kwargs['before'] = before
        kwargs['after'] = after

        log.debug('Fetching comments to post "%s" of owner "%s", offset %d' % (post.remote_id, post.wall_owner, offset))

        return self.fetch(**kwargs)

    @transaction.commit_on_success
    def fetch_group_post_parser(self, post, offset=0, count=None):  # jkj, after=None, only_new=False):
        '''
        Old method via parser
        '''
        post_data = {
            'al': 1,
            'offset': offset,
            'part': 1,
        }

        log.debug('Fetching comments to post "%s" of owner "%s", offset %d' % (post.remote_id, post.wall_owner, offset))

        parser = VkontakteWallParser().request('/wall%s' % (post.remote_id), data=post_data)

        items = parser.content_bs.findAll('div', {'class': 'fw_reply'})

        current_count = offset + len(items)
        need_cut = count and count < current_count
        if need_cut:
            items = items[:count - offset]

#        # get date of last comment and set after attribute
#        if only_new:
#            comments = post.wall_comments.order_by('-date')
#            if comments:
#                after = comments[0].date

        for item in items:

            try:
                comment = parser.parse_comment(item, post.wall_owner)
            except VkontakteParseError, e:
                log.error(e)
                continue

            comment.post = post
            comment.raw_html = unicode(item)
            comment.save()
            parsed.send(sender=Comment, instance=comment, container=item)

#            if after and comment.date < after:
#                need_cut = True
#                break

        if len(items) == 20 and not need_cut:
            return self.fetch_group_post(post, offset=current_count, count=count)  # , after=after, only_new=only_new)
#        elif after and need_cut:
#            return post.wall_comments.filter(date__gte=after)
        else:
            if not count:
                post.comments = post.wall_comments.count()
                post.save()
            return post.wall_comments.all()


class WallAbstractModel(VkontakteModel, VkontakteCRUDModel):
    class Meta:
        abstract = True

    methods_namespace = 'wall'
    slug_prefix = 'wall'
    generic_fields_models_allowed = [Group, User]
    _commit_remote = False

    remote_id = models.CharField(u'ID', max_length='20', help_text=u'Уникальный идентификатор', unique=True)

    # only for posts/comments from parser
    raw_html = models.TextField()
    raw_json = fields.JSONField(default={}, null=True)

    @property
    def slug(self):
        return self.slug_prefix + str(self.remote_id)

    @property
    def on_group_wall(self):
        return self.wall_owner_content_type == ContentType.objects.get_for_model(Group)

    @property
    def on_user_wall(self):
        return self.wall_owner_content_type == ContentType.objects.get_for_model(User)

    @property
    def by_group(self):
        return self.author_content_type == ContentType.objects.get_for_model(Group)

    @property
    def by_user(self):
        return self.author_content_type == ContentType.objects.get_for_model(User)

    @property
    def remote_owner_id(self):
        owner_id = self.wall_owner.remote_id
        if isinstance(self.wall_owner, Group) and owner_id > 0:
            owner_id *= -1
        return owner_id

    @property
    def remote_id_short(self):
        return self.remote_id.split('_')[1]

    def save(self, *args, **kwargs):
        self.prepare_generic_fields()
        return super(WallAbstractModel, self).save(*args, **kwargs)

    def prepare_generic_fields(self):
        '''
        Check and set exactly right Group or User content types, not content type of a child
        '''
        allowed_ct_pks = [ct.pk for ct in ContentType.objects.get_for_models(*self.generic_fields_models_allowed).values()]
        for field_name in self.generic_field_names:
            ct_field_name = '%s_content_type' % field_name
            for allowed_model in self.generic_fields_models_allowed:
                if isinstance(getattr(self, field_name), allowed_model):
                    setattr(self, ct_field_name, ContentType.objects.get_for_model(allowed_model))
                    break
            if getattr(self, field_name) and getattr(self, ct_field_name).pk not in allowed_ct_pks:
                raise AttributeError("Attribute '%s' field should be any of %s instance, but not %s" % (field_name, allowed_models, getattr(self, field_name)))

    def get_or_create_group_or_user(self, remote_id):
        if remote_id > 0:
            Model = User
        elif remote_id < 0:
            Model = Group
        else:
            raise ValueError("remote_id shouldn't be equal to 0")

        return Model.objects.get_or_create(remote_id=abs(remote_id))

    @transaction.commit_on_success
    def fetch_likes(self, *args, **kwargs):

#        kwargs['offset'] = int(kwargs.pop('offset', 0))
        kwargs['likes_type'] = self.likes_type
        kwargs['item_id'] = self.remote_id.split('_')[1]
        kwargs['owner_id'] = self.wall_owner.remote_id
        if isinstance(self.wall_owner, Group):
            kwargs['owner_id'] *= -1

        log.debug('Fetching likes of %s %s of owner "%s"' % (self._meta.module_name, self.remote_id, self.wall_owner))

        ids = User.remote.fetch_likes_user_ids(*args, **kwargs)
        if not ids:
            return User.objects.none()

        # fetch users
        self.like_users = User.remote.fetch(ids=ids, only_expired=True)

        # update self.likes
        likes_count = self.like_users.count()
        if likes_count < self.likes:
            log.warning('Fetched ammount of like users less, than attribute `likes` of post "%s": %d < %d' % (self.remote_id, likes_count, self.likes))
        self.likes = likes_count
        self.save()

        return self.like_users.all()


class Post(WallAbstractModel):
    class Meta:
        verbose_name = u'Сообщение Вконтакте'
        verbose_name_plural = u'Сообщения Вконтакте'

    likes_type = 'post'
    fields_required_for_update = ['post_id', 'owner_id']
    generic_field_names = ['author', 'wall_owner', 'copy_owner']

    # Владелец стены сообщения User or Group
    wall_owner_content_type = models.ForeignKey(ContentType, related_name='vkontakte_wall_posts')
    wall_owner_id = models.PositiveIntegerField(db_index=True)
    wall_owner = generic.GenericForeignKey('wall_owner_content_type', 'wall_owner_id')

    # Создатель/автор сообщения
    author_content_type = models.ForeignKey(ContentType, related_name='vkontakte_posts')
    author_id = models.PositiveIntegerField(db_index=True)
    author = generic.GenericForeignKey('author_content_type', 'author_id')

    # abstract field for correct deleting group and user models in admin
    group_wall = generic.GenericForeignKey('wall_owner_content_type', 'wall_owner_id')
    user_wall = generic.GenericForeignKey('wall_owner_content_type', 'wall_owner_id')
    group = generic.GenericForeignKey('author_content_type', 'author_id')
    user = generic.GenericForeignKey('author_content_type', 'author_id')

    date = models.DateTimeField(u'Время сообщения', db_index=True)
    text = models.TextField(u'Текст записи')

    comments = models.PositiveIntegerField(u'Кол-во комментариев', default=0, db_index=True)
    likes = models.PositiveIntegerField(u'Кол-во лайков', default=0, db_index=True)
    reposts = models.PositiveIntegerField(u'Кол-во репостов', default=0, db_index=True)

    like_users = ManyToManyHistoryField(User, related_name='like_posts')
    repost_users = ManyToManyHistoryField(User, related_name='repost_posts')

    #{u'photo': {u'access_key': u'5f19dfdc36a1852824',
    #u'aid': -7,
    #u'created': 1333664090,
    #u'height': 960,
    #u'owner_id': 2462759,
    #u'pid': 281543621,
    #u'src': u'http://cs9733.userapi.com/u2462759/-14/m_fdad45ec.jpg',
    #u'src_big': u'http://cs9733.userapi.com/u2462759/-14/x_60b1aed1.jpg',
    #u'src_small': u'http://cs9733.userapi.com/u2462759/-14/s_d457021e.jpg',
    #u'src_xbig': u'http://cs9733.userapi.com/u2462759/-14/y_b5a67b8d.jpg',
    #u'src_xxbig': u'http://cs9733.userapi.com/u2462759/-14/z_5a64a153.jpg',
    #u'text': u'',
    #u'width': 1280},
    #u'type': u'photo'}

    #u'attachments': [{u'link': {u'description': u'',
    #u'image_src': u'http://cs6030.userapi.com/u2462759/-2/x_cb9c00f8.jpg',
    #u'title': u'SAAB_9000_CD_2_0_Turbo_190_k.jpg',
    #u'url': u'http://www.yauto.cz/includes/img/inzerce/SAAB_9000_CD_2_0_Turbo_190_k.jpg'},
    #u'type': u'link'}],
    #attachments - содержит массив объектов, которые присоединены к текущей записи (фотографии, ссылки и т.п.). Более подробная информация представлена на странице Описание поля attachments
    attachments = models.TextField()
    media = models.TextField()

    #{u'coordinates': u'55.6745689498 37.8724562529',
    #u'place': {u'city': u'Moskovskaya oblast',
    #u'country': u'Russian Federation',
    #u'title': u'Shosseynaya ulitsa, Moskovskaya oblast'},
    #u'type': u'point'}
    #geo - если в записи содержится информация о местоположении, то она будет представлена в данном поле. Более подробная информация представлена на странице Описание поля geo
    geo = models.TextField()

    signer_id = models.PositiveIntegerField(null=True, help_text=u'Eсли запись была опубликована от имени группы и подписана пользователем, то в поле содержится идентификатор её автора')

    copy_owner_content_type = models.ForeignKey(ContentType, related_name='vkontakte_wall_copy_posts', null=True)
    copy_owner_id = models.PositiveIntegerField(null=True, db_index=True, help_text=u'Eсли запись является копией записи с чужой стены, то в поле содержится идентификатор владельца стены у которого была скопирована запись')
    copy_owner = generic.GenericForeignKey('copy_owner_content_type', 'copy_owner_id')

    # TODO: rename wall_reposts -> reposts, after renaming reposts -> reposts_count
    copy_post = models.ForeignKey('Post', null=True, related_name='wall_reposts', help_text=u'Если запись является копией записи с чужой стены, то в поле содержится идентфикатор скопированной записи на стене ее владельца')
#     copy_post_date = models.DateTimeField(u'Время сообщения-оригинала', null=True)
#     copy_post_type = models.CharField(max_length=20)
    copy_text = models.TextField(u'Комментарий при репосте', help_text=u'Если запись является копией записи с чужой стены и при её копировании был добавлен комментарий, его текст содержится в данном поле')

    # not in API
    post_source = models.TextField()
    online = models.PositiveSmallIntegerField(null=True)
    reply_count = models.PositiveIntegerField(null=True)

    objects = VkontakteCRUDManager()
    remote = PostRemoteManager(remote_pk=('remote_id',), methods={
        'get': 'get',
        'getById': 'getById',
        'create': 'post',
        'update': 'edit',
        'delete': 'delete',
        'restore': 'restore',
    })

    @property
    def reposters(self):
        return [repost.author for repost in self.wall_reposts.all()]

    def __unicode__(self):
        return '%s: %s' % (unicode(self.wall_owner), self.text)

    def save(self, *args, **kwargs):
        # check strings for good encoding
        # there is problems to save users with bad encoded activity strings like user ID=88798245

#        try:
#            self.text.encode('utf-16').decode('utf-16')
#        except UnicodeDecodeError:
#            self.text = ''

        # поле назначено через API
        if self.copy_owner_id and not self.copy_owner_content_type:
            ct_model = User if self.copy_owner_id > 0 else Group
            self.copy_owner_content_type = ContentType.objects.get_for_model(ct_model)
            self.copy_owner = ct_model.remote.fetch(ids=[abs(self.copy_owner_id)])[0]

        # save generic fields before saving post
        if self.copy_owner:
            self.copy_owner.save()

        return super(Post, self).save(*args, **kwargs)

    def prepare_create_params(self, **kwargs):
        kwargs.update({
            'owner_id': self.remote_owner_id,
            'friends_only': kwargs.get('friends_only', 0),
            'from_group': kwargs.get('from_group', ''),
            'message': self.text,
            'attachments': self.attachments,
            'services': kwargs.get('services', ''),
            'signed': 1 if self.signer_id else 0,
            'publish_date': kwargs.get('publish_date', ''),
            'lat': kwargs.get('lat', ''),
            'long': kwargs.get('long', ''),
            'place_id': kwargs.get('place_id', ''),
            'post_id': kwargs.get('post_id', '')
        })
        return kwargs

    def prepare_update_params(self, **kwargs):
        return self.prepare_create_params(post_id=self.remote_id_short, **kwargs)

    def prepare_delete_params(self):
        return {
            'owner_id': self.remote_owner_id,
            'post_id': self.remote_id_short
        }

    def parse_remote_id_from_response(self, response):
        if response:
            return '%s_%s' % (self.remote_owner_id, response['post_id'])
        return None

    def parse(self, response):
        self.raw_json = dict(response)

        for field_name in ['comments', 'likes', 'reposts']:
            if field_name in response and 'count' in response[field_name]:
                setattr(self, field_name, response.pop(field_name)['count'])

        # TODO: may we should move this to save and keep parse queryless
        self.wall_owner = self.get_or_create_group_or_user(response.pop('to_id'))[0]
        self.author = self.get_or_create_group_or_user(response.pop('from_id'))[0]

        response.pop('attachment', {})
        for attachment in response.pop('attachments', []):
            pass
#            if attachment['type'] == 'poll':
                # это можно делать только после сохранения поста, так что тольо через сигналы
#               self.fetch_poll(attachment['poll']['poll_id'])

        # TODO: this block broke tests with error
        # IntegrityError: new row for relation "vkontakte_wall_post" violates check constraint "vkontakte_wall_post_copy_owner_id_check"
#         if response.get('copy_owner_id'):
#             try:
#                 self.copy_owner_content_type = ContentType.objects.get_for_model(User if response.get('copy_owner_id') > 0 else Group)
#                 self.copy_owner = self.copy_owner_content_type.get_object_for_this_type(remote_id=abs(response.get('copy_owner_id')))
#                 if response.get('copy_post_id'):
#                     self.copy_post = Post.objects.get(remote_id='%s_%s' % (response.get('copy_owner_id'), response.get('copy_post_id')))
#             except ObjectDoesNotExist:
#                 pass

        super(Post, self).parse(response)

        self.remote_id = '%s%s_%s' % (('-' if self.on_group_wall else ''), self.wall_owner.remote_id, self.remote_id)

    def fetch_comments(self, *args, **kwargs):
        return Comment.remote.fetch_post(post=self, *args, **kwargs)

    @transaction.commit_on_success
    def fetch_likes(self, source='api', *args, **kwargs):
        if source == 'api':
            return super(Post, self).fetch_likes(*args, **kwargs)
        else:
            return self.fetch_likes_parser(*args, **kwargs)

    @transaction.commit_on_success
    def fetch_likes_parser(self, offset=0):
        '''
        Update and save fields:
            * likes - count of likes
        Update relations:
            * like_users - users, who likes this post
        '''
        post_data = {
            'act': 'show',
            'al': 1,
            'w': 'likes/wall%s' % self.remote_id,
        }

        if offset == 0:
            number_on_page = 120
            post_data['loc'] = 'wall%s' % self.remote_id,
        else:
            number_on_page = 60
            post_data['offset'] = offset

        log.debug('Fetching likes of post "%s" of owner "%s", offset %d' % (self.remote_id, self.wall_owner, offset))

        parser = VkontakteWallParser().request('/wkview.php', data=post_data)

        if offset == 0:
            try:
                self.likes = int(parser.content_bs.find('a', {'id': 'wk_likes_tablikes'}).find('nobr').text.split()[0])
                self.save()
            except ValueError:
                return
            except:
                log.warning('Strange markup of first page likes response: "%s"' % parser.content)
            self.like_users.clear()

        #<div class="wk_likes_liker_row inl_bl" id="wk_likes_liker_row722246">
        #  <div class="wk_likes_likerph_wrap" onmouseover="WkView.likesBigphOver(this, 722246)">
        #    <a class="wk_likes_liker_ph" href="/kicolenka">
        #      <img class="wk_likes_liker_img" src="http://cs418825.vk.me/v418825246/6cf8/IBbSfmDz6R8.jpg" width="100" height="100" />
        #    </a>
        #  </div>
        #  <div class="wk_likes_liker_name"><a class="wk_likes_liker_lnk" href="/kicolenka">Оля Киселева</a></div>
        #</div>

        items = parser.add_users(users=('div', {'class': re.compile(r'^wk_likes_liker_row')}),
            user_link=('a', {'class': 'wk_likes_liker_lnk'}),
            user_photo=('img', {'class': 'wk_likes_liker_img'}),
            user_add=lambda user: self.like_users.add(user))

        if len(items) == number_on_page:
            self.fetch_likes_parser(offset=offset + number_on_page)
        else:
            return self.like_users.all()

    def fetch_reposts(self, source='api', *args, **kwargs):
        if source == 'api':
            return self.fetch_reposts_api(*args, **kwargs)
        else:
            return self.fetch_reposts_parser(*args, **kwargs)

    def fetch_reposts_api(self, *args, **kwargs):
        self.fetch_instance_reposts(*args, **kwargs)

        # update self.reposts
        reposts_count = self.repost_users.get_query_set(only_pk=True).count()
        if reposts_count < self.reposts:
            log.warning('Fetched ammount of repost users less, than attribute `reposts` of post "%s": %d < %d' % (self.remote_id, reposts_count, self.reposts))
        self.reposts = reposts_count
        self.save()

        return self.repost_users.all()

    @transaction.commit_on_success
    def fetch_instance_reposts(self, *args, **kwargs):

        resources = self.fetch_repost_items(*args, **kwargs)
        if not resources:
            return Post.objects.none()

        # TODO: still complicated to store reposts objects, may be it's task for another application
#         posts = Post.remote.parse_response(resources)#, extra_fields={'copy_post_id': self.pk})
#         return Post.objects.filter(pk__in=set([Post.remote.get_or_create_from_instance(instance).pk for instance in posts]))

        # positive ids -> only users
        # TODO: think about how to store reposts by groups
        timestamps = dict([(post['from_id'], post['date']) for post in resources if post['from_id'] > 0])
        ids_new = timestamps.keys()
        ids_current = self.repost_users.get_query_set(only_pk=True).using(MASTER_DATABASE).exclude(time_from=None)
        ids_add = set(ids_new).difference(set(ids_current))
        ids_remove = set(ids_current).difference(set(ids_new))

        m2m_model = self.repost_users.through

        # fetch new users
        User.remote.fetch(ids=ids_add, only_expired=True)

        # remove old reposts without time_from
        self.repost_users.get_query_set_through().filter(time_from=None).delete()

        # add new reposts
        get_repost_date = lambda id: datetime.fromtimestamp(timestamps[id]) if id in timestamps else self.date
        m2m_model.objects.bulk_create([m2m_model(**{'user_id': id, 'post_id': self.pk, 'time_from': get_repost_date(id)}) for id in ids_add])

        # remove reposts.
        # Commented becouse of .using(MASTER_DATABASE).exclude(time_from=None) filtering for ids_current
#        m2m_model.objects.filter(post_id=self.pk, user_id__in=ids_remove).update(time_to=datetime.now())
        return

    # не рекомендуется указывать default_count из-за бага паджинации репостов: https://vk.com/wall-51742963_6860
    @fetch_all
    def fetch_repost_items(self, offset=0, count=1000, *args, **kwargs):
        if count > 1000:
            raise ValueError("Parameter 'count' can not be more than 1000")

        # owner_id
        # идентификатор пользователя или сообщества, на стене которого находится запись. Если параметр не задан, то он считается равным идентификатору текущего пользователя.
        # Обратите внимание, идентификатор сообщества в параметре owner_id необходимо указывать со знаком "-" — например, owner_id=-1 соответствует идентификатору сообщества ВКонтакте API (club1)
        kwargs['owner_id'] = self.wall_owner.remote_id
        if isinstance(self.wall_owner, Group):
            kwargs['owner_id'] *= -1
        # post_id
        # идентификатор записи на стене.
        kwargs['post_id'] = self.remote_id.split('_')[1]
        # offset
        # смещение, необходимое для выборки определенного подмножества записей.
        kwargs['offset'] = int(offset)
        # count
        # количество записей, которое необходимо получить.
        # положительное число, по умолчанию 20, максимальное значение 100
        kwargs['count'] = int(count)

        log.debug('Fetching repost users ids of post %s, offset %d' % (self.remote_id, offset))

        response = api_call('wall.getReposts', **kwargs)
        return response['items']

    @transaction.commit_on_success
    def fetch_reposts_parser(self, offset=0):
        '''
        OLD method via parser, may works incorrect
        Update and save fields:
            * reposts - count of reposts
        Update relations
            * repost_users - users, who repost this post
        '''
        post_data = {
            'act': 'show',
            'al': 1,
            'w': 'shares/wall%s' % self.remote_id,
        }

        if offset == 0:
            number_on_page = 40
            post_data['loc'] = 'wall%s' % self.remote_id,
        else:
            number_on_page = 20
            post_data['offset'] = offset

        log.debug('Fetching reposts of post "%s" of owner "%s", offset %d' % (self.remote_id, self.wall_owner, offset))

        parser = VkontakteWallParser().request('/wkview.php', data=post_data)
        if offset == 0:
            try:
                self.reposts = int(parser.content_bs.find('a', {'id': 'wk_likes_tabshares'}).find('nobr').text.split()[0])
                self.save()
            except ValueError:
                return
            except:
                log.warning('Strange markup of first page shares response: "%s"' % parser.content)
            self.repost_users.clear()

        #<div id="post65120659_2341" class="post post_copy" onmouseover="wall.postOver('65120659_2341')" onmouseout="wall.postOut('65120659_2341')" data-copy="-16297716_126261" onclick="wall.postClick('65120659_2341', event)">
        #  <div class="post_table">
        #    <div class="post_image">
        #      <a class="post_image" href="/vano0ooooo"><img src="/images/camera_c.gif" width="50" height="50"/></a>
        #    </div>
        #      <div class="wall_text"><a class="author" href="/vano0ooooo" data-from-id="65120659">Иван Панов</a> <div id="wpt65120659_2341"></div><table cellpadding="0" cellspacing="0" class="published_by_wrap">

        items = parser.add_users(users=('div', {'id': re.compile('^post\d'), 'class': re.compile('^post ')}),
            user_link=('a', {'class': 'author'}),
            user_photo=lambda item: item.find('a', {'class': 'post_image'}).find('img'),
            user_add=lambda user: self.repost_users.add(user))

        if len(items) == number_on_page:
            self.fetch_reposts(offset=offset + number_on_page)
        else:
            return self.repost_users.all()

    def fetch_statistic(self, *args, **kwargs):
        if 'vkontakte_wall_statistic' not in settings.INSTALLED_APPS:
            raise ImproperlyConfigured("Application 'vkontakte_wall_statistic' not in INSTALLED_APPS")

        from vkontakte_wall_statistic.models import PostStatistic
        return PostStatistic.remote.fetch(post=self, *args, **kwargs)

class Comment(WallAbstractModel):
    class Meta:
        verbose_name = u'Коментарий сообщения Вконтакте'
        verbose_name_plural = u'Комментарии сообщений Вконтакте'

    remote_pk_field = 'cid'
    likes_type = 'comment'
    fields_required_for_update = ['comment_id', 'post_id', 'owner_id']
    generic_field_names = ['reply_for', 'author', 'wall_owner']

    post = models.ForeignKey(Post, verbose_name=u'Пост', related_name='wall_comments')

    # Владелец стены сообщения User or Group (декомпозиция от self.post для фильтра в админке и быстрых запросов)
    wall_owner_content_type = models.ForeignKey(ContentType, related_name='vkontakte_wall_comments')
    wall_owner_id = models.PositiveIntegerField(db_index=True)
    wall_owner = generic.GenericForeignKey('wall_owner_content_type', 'wall_owner_id')

    # Автор комментария
    author_content_type = models.ForeignKey(ContentType, related_name='comments')
    author_id = models.PositiveIntegerField(db_index=True)
    author = generic.GenericForeignKey('author_content_type', 'author_id')

    from_id = models.IntegerField(null=True)  # strange value, seems to be equal to author

    # Это ответ пользователю
    reply_for_content_type = models.ForeignKey(ContentType, null=True, related_name='replies')
    reply_for_id = models.PositiveIntegerField(null=True, db_index=True)
    reply_for = generic.GenericForeignKey('reply_for_content_type', 'reply_for_id')

    reply_to = models.ForeignKey('self', null=True, verbose_name=u'Это ответ на комментарий')

    # abstract field for correct deleting group and user models in admin
    group = generic.GenericForeignKey('author_content_type', 'author_id')
    user = generic.GenericForeignKey('author_content_type', 'author_id')
    group_wall_reply = generic.GenericForeignKey('reply_for_content_type', 'reply_for_id')
    user_wall_reply = generic.GenericForeignKey('reply_for_content_type', 'reply_for_id')

    date = models.DateTimeField(u'Время комментария', db_index=True)
    text = models.TextField(u'Текст комментария')

    likes = models.PositiveIntegerField(u'Кол-во лайков', default=0, db_index=True)

    like_users = ManyToManyHistoryField(User, related_name='like_comments')

    objects = VkontakteCRUDManager()
    remote = CommentRemoteManager(remote_pk=('remote_id',), methods={
        'get': 'getComments',
        'create': 'addComment',
        'update': 'editComment',
        'delete': 'deleteComment',
        'restore': 'restoreComment',
    })

    def save(self, *args, **kwargs):
        self.wall_owner = self.post.wall_owner
        return super(Comment, self).save(*args, **kwargs)

    def prepare_create_params(self, **kwargs):
        kwargs.update({
            'owner_id': self.remote_owner_id,
            'post_id': self.post.remote_id_short,
            'text': self.text,
            'reply_to_comment': self.reply_for.id if self.reply_for else '',
            'from_group': int(kwargs.get('from_group', 0)),
            'attachments': kwargs.get('attachments', ''),
        })
        return kwargs

    def prepare_update_params(self, **kwargs):
        kwargs.update({
            'owner_id': self.remote_owner_id,
            'comment_id': self.remote_id_short,
            'message': self.text,
            'attachments': kwargs.get('attachments', ''),
        })
        return kwargs

    def prepare_delete_params(self):
        return {
            'owner_id': self.remote_owner_id,
            'comment_id': self.remote_id_short
        }

    def parse_remote_id_from_response(self, response):
        if response:
            return '%s_%s' % (self.remote_owner_id, response['cid'])
        return None

    def parse(self, response):
        self.raw_json = response
        super(Comment, self).parse(response)

        if '_' not in str(self.remote_id):
            self.remote_id = '%s_%s' % (self.post.remote_id.split('_')[0], self.remote_id)

        for field_name in ['likes']:
            if field_name in response and 'count' in response[field_name]:
                setattr(self, field_name, response.pop(field_name)['count'])

        self.author = User.objects.get_or_create(remote_id=response['uid'])[0]

        if 'reply_to_uid' in response:
            self.reply_for = User.objects.get_or_create(remote_id=response['reply_to_uid'])[0]
        if 'reply_to_cid' in response:
            try:
                self.reply_to = Comment.objects.get(remote_id=response['reply_to_cid'])
            except:
                pass

Group.add_to_class('wall_posts', generic.GenericRelation(Post, content_type_field='wall_owner_content_type', object_id_field='wall_owner_id', related_name='group_wall', verbose_name=u'Сообщения на стене'))
User.add_to_class('wall_posts', generic.GenericRelation(Post, content_type_field='wall_owner_content_type', object_id_field='wall_owner_id', related_name='user_wall', verbose_name=u'Сообщения на стене'))

Group.add_to_class('posts', generic.GenericRelation(Post, content_type_field='author_content_type', object_id_field='author_id', related_name='group', verbose_name=u'Сообщения'))
User.add_to_class('posts', generic.GenericRelation(Post, content_type_field='author_content_type', object_id_field='author_id', related_name='user', verbose_name=u'Сообщения'))

Group.add_to_class('comments', generic.GenericRelation(Comment, content_type_field='author_content_type', object_id_field='author_id', related_name='group', verbose_name=u'Комментарии'))
User.add_to_class('comments', generic.GenericRelation(Comment, content_type_field='author_content_type', object_id_field='author_id', related_name='user', verbose_name=u'Комментарии'))

Group.add_to_class('replies', generic.GenericRelation(Comment, content_type_field='reply_for_content_type', object_id_field='reply_for_id', related_name='group_wall_reply', verbose_name=u'Ответы на комментарии'))
User.add_to_class('replies', generic.GenericRelation(Comment, content_type_field='reply_for_content_type', object_id_field='reply_for_id', related_name='user_wall_reply', verbose_name=u'Ответы на комментарии'))
