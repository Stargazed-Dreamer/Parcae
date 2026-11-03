"""通用图像/OCR 结果工具——忠实移植自舟项目 <data_drive>:/<projects_root>\\舟\\combine\\tool.py 的 Tool 类。

仅做两处适配，其余（函数名、参数名、实现逻辑、默认值）与原实现**逐行一致**：

1. 去业务依赖：原版 `from log import log_manager` 换成 logging stub；
   OCR 方法（init_ocr/ocr，依赖 PaddleOCR）与业务方法（getTag/getAgent/
   getHistory，依赖 DATA）未迁移。
2. find_centerOnResult 抛的 CantFindNameError 原本定义在舟项目
   core/exceptions.py，此处本地定义同名类。

另有三处**运行期等价**的写法调整（只为过 pyright，算法与参数不变）：

- `cv2.dilate(edges, None)` → 显式 3×3 kernel（cv2 对 None 的默认值就是它）；
- NMS 后的 `indices` 统一 `np.asarray(..., intp).reshape(-1)`，替代
  `hasattr('flatten')` 分支（不同 OpenCV 版本返回 (N,) / (N,1) / tuple）；
- `returnResult(similarity=1)` → `similarity: float = 1.0`（调用方传的是 float 余弦值）。

使用::

    from tools.img_tools import Tool
    t = Tool()
    hits = t.find_imgOnImg(screen, tpl)      # [([x1,y1,x2,y2], score), ...]
    btns = t.find_smallRegionsOnImg(screen)  # [crop, ...]
    name = t.find_nameOnResult(ocr_result, "确认", mode="result")
"""

from __future__ import annotations

import cv2
import numpy as np

# 环境适配 1/2：轻量日志（原版 from log import log_manager as log）
try:
    from log import log_manager as log  # type: ignore
except ImportError:
    import logging

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    log = logging.getLogger("img_tools")


class CantFindNameError(Exception):
    """环境适配 2/2：原定义在舟项目 core/exceptions.py，此处同名本地定义。"""


class Tool:
    def __init__(self):
        """
        初始化工具类
        """
        self.ocrUseable = False

    def find_smallRegionsOnImg(self, image):
        """
        找出并裁剪UI图片中的按钮区域，结合颜色识别和边缘检测
        Args:
            image: OpenCV格式的图片(BGR)
        Returns:
            list: 裁剪图像列表
        """
        # 1. 颜色识别部分
        # 转换到HSV空间
        hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)

        # 定义目标颜色 #313131 的HSV范围
        # 增大颜色容差范围
        lower = np.array([0, 0, 30])  # 更宽松的下限
        upper = np.array([180, 30, 80])  # 更宽松的上限

        # 创建颜色掩码
        color_mask = cv2.inRange(hsv, lower, upper)

        # 2. 边缘检测部分
        # 转换为灰度图
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)

        # 高斯模糊减少噪声
        blurred = cv2.GaussianBlur(gray, (5, 5), 0)

        # Canny边缘检测（调整阈值）
        edges = cv2.Canny(blurred, 30, 100)  # 降低阈值使边缘更容易被检测

        # 3. 组合颜色掩码和边缘
        # 膨胀边缘使其更容易形成闭合区域
        # kernel 显式给 3×3：cv2 对 kernel=None 的默认值就是 MORPH_RECT 3×3，行为等价，
        # 但 stub 不允许 None（reportCallIssue）
        dilated_edges = cv2.dilate(edges, np.ones((3, 3), np.uint8), iterations=3)  # 增加膨胀次数

        # 合并颜色掩码和边缘
        combined_mask = cv2.bitwise_or(color_mask, dilated_edges)  # 使用or而不是and

        # 4. 形态学处理
        kernel = np.ones((3, 3), np.uint8)  # 减小核的大小
        mask = cv2.morphologyEx(combined_mask, cv2.MORPH_CLOSE, kernel, iterations=2)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)

        # 5. 查找轮廓
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        # 存储裁剪后的图像
        l_output = []

        # 6. 处理每个轮廓
        for contour in contours:
            # 计算轮廓面积
            area = cv2.contourArea(contour)

            # 根据面积和宽高比筛选
            if area > 0.05 * image.shape[0] * image.shape[1]:  # 暂时只用面积过滤
                x, y, w, h = cv2.boundingRect(contour)

                # 稍微扩大裁剪区域，确保不会裁掉边缘
                padding = 2
                x = max(0, x - padding)
                y = max(0, y - padding)
                w = min(image.shape[1] - x, w + 2 * padding)
                h = min(image.shape[0] - y, h + 2 * padding)

                # 裁剪图像
                l_output.append(self.cropping(image, (x, y, w, h), format="x, y, w, h"))
        return l_output

    def find_imgOnImg(self, mainImg, template, match_threshold=0.7, scales=np.linspace(0.8, 1.2, 5), nms_threshold=0.4, b_needTemplate2GRAY=True):
        """
        图片匹配函数，支持多尺度和非极大值抑制

        参数:
        mainImg: numpy.ndarray - BGR格式的屏幕截图（h, w, 3）
        template: numpy.ndarray - BGR格式的模板图片（h, w, 3）
        match_threshold: float - 匹配置信度阈值（默认0.7）
        scales: list - 缩放比例列表（默认[0.8, 0.9, 1.0, 1.1, 1.2]）
        nms_threshold: float - NMS重叠阈值（默认0.4）

        返回:
        list - [([x1,y1,x2,y2], confidence), ...]
        """

        # 转换为灰度图
        mainImg_gray = cv2.cvtColor(mainImg, cv2.COLOR_BGR2GRAY)
        if b_needTemplate2GRAY:
            template_gray = cv2.cvtColor(template, cv2.COLOR_BGR2GRAY)
        else:
            template_gray = template

        t_h, t_w = template_gray.shape
        s_h, s_w = mainImg_gray.shape

        matches = []

        # 多尺度匹配
        for scale in scales:
            # 计算缩放后模板尺寸
            scaled_w = int(t_w * scale)
            scaled_h = int(t_h * scale)

            # 跳过无效尺寸
            if scaled_w < 5 or scaled_h < 5 or scaled_w > s_w or scaled_h > s_h:
                continue

            # 调整模板尺寸
            resized = cv2.resize(template_gray, (scaled_w, scaled_h),
                                 interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_CUBIC)

            # 模板匹配
            result = cv2.matchTemplate(mainImg_gray, resized, cv2.TM_CCOEFF_NORMED)

            # 获取匹配结果
            loc = np.where(result >= match_threshold)
            for pt in zip(*loc[::-1]):  # 交换x,y坐标
                x1, y1 = pt
                x2, y2 = x1 + scaled_w, y1 + scaled_h
                confidence = result[y1, x1]  # 注意numpy数组的行列顺序
                matches.append(([x1, y1, x2, y2], float(confidence)))

        # 非极大值抑制
        if not matches:
            return []

        boxes = np.array([m[0] for m in matches])
        confidences = np.array([m[1] for m in matches])

        # 使用OpenCV的NMS实现
        indices = cv2.dnn.NMSBoxes(
            boxes.reshape(-1, 4).tolist(),
            confidences.tolist(),
            score_threshold=match_threshold,
            nms_threshold=nms_threshold
        )

        # 处理 OpenCV 版本差异：NMSBoxes 返回值在不同版本是 (N,) / (N,1) / tuple，
        # np.asarray + reshape(-1) 一次盖掉所有形状（原版用 hasattr('flatten') 分支判断，
        # stub 里返回类型标的是 Sequence[int]，两个分支都报类型错）
        indices = np.asarray(indices, dtype=np.intp).reshape(-1)
        if indices.size == 0:
            return []

        final_matches = [matches[i] for i in indices]
        # 返回排序后的结果（按置信度降序）
        final_matches.sort(key=lambda x: -x[1])

        return final_matches

    def cropping(self, cvImg, region, mode="pixel", format="x1, y1, x2, y2"):
        """
        裁剪图片大小到指定区域，支持显示缩放
        Args:
            cvImg:  图片数组
            region: 一个四元数组，内容取决于format参数，如：x, y, w, h -> (x, y, width【图片相对大小】, height【图片相对大小】) 相对于窗口的坐标和大小
            mode: "pixel" 表示以像素为单位，"percent" 表示以百分比为单位
            format: "x, y, w, h" | 1   或者   "x1, y1, x2, y2" | 2
        """
        a, b, c, d = region
        if format == 1 or format == "x, y, w, h":
            x1, y1, x2, y2 = a, b, a + c, b + d
        elif format == 2 or format == "x1, y1, x2, y2":
            x1, y1, x2, y2 = a, b, c, d
        else:
            raise ValueError(f"'{format}'格式不是合法的值")
        if mode == "percent":
            shape = cvImg.shape  # (height, width, channels)
            x1, y1, x2, y2 = shape[1] * a, shape[0] * b, shape[1] * c, shape[0] * d
        elif mode == "pixel":
            "Good"
        else:
            raise ValueError(f"'{mode}'处理模式不是合法的值")
        x1, y1, x2, y2 = int(x1), int(y1), int(x2), int(y2)
        # 确保裁剪区域有效
        if x1 >= x2 or y1 >= y2:
            raise ValueError(f"裁剪区域无效，起始坐标 {(x1, y1)} 必须小于结束坐标 {(x2, y2)}")
        if x1 < 0 or x2 < 0 or y1 < 0 or y2 < 0:
            raise ValueError(f"裁剪区域无效，坐标 {(x1, y1)} 或坐标 {(x2, y2)} 中有负数")
        return cvImg[y1:y2, x1:x2]

    def showImg(self, img):
        cv2.imshow("picture", img)
        cv2.waitKey(0)
        cv2.destroyAllWindows()

    def cosine_similarity(self, s1, s2):
        """
        计算两个字符串的余弦相似度。

        该方法通过统计字符频率，构建字符向量，并计算它们的余弦值来评估相似性。
        会先将字符串转为小写，并去除可能的"new"前缀。

        参数:
            s1 (str): 第一个输入字符串。
            s2 (str): 第二个输入字符串。

        返回:
            float: 两个字符串的余弦相似度，范围在0到1之间。
                   如果任一字符串为空，返回0；若字符串完全相同（忽略大小写），返回1。
        """
        if s1 == "" or s2 == "":
            # 如果任一字符串为空，则相似度为0
            return 0
        # 将两个字符串都转换为小写，以忽略大小写差异
        s1 = s1.lower()
        s2 = s2.lower()
        if s1 == s2:
            # 转换后的字符串完全相同，则相似度为1
            return 1
        # 如果字符串以"new"开头，则去除前缀"new"再进行后续计算
        if s1[:3] == "new":
            s1 = s1[3:]
        if s2[:3] == "new":
            s2 = s2[3:]
        # 获取两个字符串中所有不重复的字符，作为向量的维度
        chars = list(set(s1 + s2))
        # 构建字符串s1和s2在字符列表chars上的频率向量
        v1 = [s1.count(c) for c in chars]
        v2 = [s2.count(c) for c in chars]

        # 计算两个向量的点积
        dot_product = sum(a * b for a, b in zip(v1, v2))
        # 计算两个向量的L2范数（长度）
        norm1 = sum(a * a for a in v1) ** 0.5
        norm2 = sum(b * b for b in v2) ** 0.5
        # 计算余弦相似度，若范数之积为0则返回0以避免除零错误
        result = dot_product / (norm1 * norm2) if norm1 * norm2 > 0 else 0.0
        return result

    def find_centerOnResult(self, result, mode=None):
        """
        mode 是输入坐标的表示方法
        1: x1, x2, y1, y2
        2: x1, y1, x2, y2
        3: paddle [[左上, 右上, 右下, 左下], (name, threshold)]
        4: x, y, w, h
        """
        if mode is None:
            raise ValueError("必须填入mode！")
        log.debug(f"找中心: {result}")
        if result is None:
            raise CantFindNameError(f"找不到None的中心!")
        l_coordinate = result[0]

        if mode == 1 or mode == "x1, x2, y1, y2":
            x1, x2, y1, y2 = l_coordinate
        elif mode == 2 or mode == "x1, y1, x2, y2":
            x1, y1, x2, y2 = l_coordinate
        elif mode == 3 or mode == "paddle":
            x1, y1 = l_coordinate[0]
            x2, y2 = l_coordinate[2]
        elif mode == 4 or mode == "x, y, w, h":
            x, y, w, h = l_coordinate
            x1, y1 = x, y
            x2, y2 = x + w, y + h
        else:
            raise ValueError("无效的mode值！")

        # 计算中心点
        center_x = (x1 + x2) // 2
        center_y = (y1 + y2) // 2

        return (center_x, center_y)

    def find_nameOnResult(self, l_result, t_name, mode="result", sameOnly=False, includeSimilarity=False):
        # mode : result or src
        # similarity 标 float：cosine_similarity 返回 float，默认值 1.0 与原来返回的 1
        # 数值相等，只在 includeSimilarity=True 时才被用
        def returnResult(result, similarity: float = 1.0):
            if mode == "result":
                output = result
            elif mode == "src":
                output = returnItem
            else:
                raise NameError(f"mode 必须是 result 或者 src, 而不是'{mode}'")
            if includeSimilarity:
                output = (output, similarity)
            log.debug(f"返回结果：{output}，相似度：{similarity}")
            return output

        returnItem = t_name
        if isinstance(t_name, str):
            t_name = (t_name,)
        elif not isinstance(t_name, tuple):
            raise ValueError(f"t_name必须是字符串或元组，当前类型为{type(t_name)}")

        # 全字匹配
        for result in l_result:
            for name in t_name:
                if result[1][0] == name:
                    return returnResult(result)

        if sameOnly:
            log.debug("仅完全匹配模式，未找到匹配项，返回None")
            return None

        # 余弦相似匹配
        maxCosine = -1
        bestResult = None
        for result in l_result:
            for name in t_name:
                similarity = self.cosine_similarity(name, result[1][0])
                if similarity > maxCosine and similarity > 0.5:
                    maxCosine = similarity
                    bestResult = result
        if maxCosine > 0.5 and bestResult is not None:
            return returnResult(bestResult, maxCosine)

        # 不完全匹配
        for result in l_result:
            for name in t_name:
                if name in result[1][0]:
                    return returnResult(result, 0.01)

        return None
